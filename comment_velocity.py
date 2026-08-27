"""
comment_count2/thread_count/comment_account の Turso版計算。

以前は supabase_record/comment_count.py が YouTube commentThreads.list を毎時
直接ライブクロールして計算していたが、comment_sync が維持している Turso
`comments` テーブルには同じ生データが既に入っているため、そちらから計算する
(flaskr/turso_stats.py が同じ理由でライブ読み取りを Turso 読みに置き換えた
パターンと同じ)。YouTube APIを一切消費しない。

定義は supabase_record/comment_count.py と同一:
  「直近 hours 時間以内のスレッド＋その全返信」
  +「hours〜extended_hours 時間前のスレッドへの、直近 hours 時間以内の返信のみ」
  （スレッド自体は対象外）

削除済み(is_deleted=1)は元実装のライブクロールが実質的に見ない状態と同じに
揃えるため除外する。

「直近1時間以内のスレッドの返信は、返信の投稿時刻を問わず全部数える」のは
手抜きではない: 返信は必ず親スレッドの投稿より後に発生するため、親スレッドが
直近1時間以内なら、その返信も自動的に直近1時間以内になる(時系列上ありえない
順序を除けば矛盾しない)。よって「全返信」と「直近1時間以内の返信」は
この場合常に同じ集合になる。

【固定コメント対策について(2026-07-28)】
元の supabase_record/comment_count.py には特定の固定コメント本文全体との
完全一致だけを条件にした、説明コメント無しの文字列マッチがあった。これは
「スパム除外」ではなく、実際にはこの動画の固定コメント本文そのものを直接
ハードコードした固定コメント除外策だった(動画投稿者に確認して判明。詳細は
AGENTS.md の Non-obvious Implementation Details 参照)。YouTube API の commentThreads.list(order="time") はページネーション
順として固定コメントを常に先頭に返す(実際の投稿時刻に関係なく)ため、ライブ
クロールではこれを識別する手段がAPI上に無く、既知の固定コメント文言との
文字列一致という場当たり的な方法に頼っていた。

このTurso版はライブAPIのページネーション順ではなく Turso `comments.published_at`
(UPSERT時も上書きされない、投稿時点の実際の値)を直接 `WHERE published_at >= ?`
で絞り込むため、「常に先頭に来る」という問題自体が発生しない。固定コメントの
実際の投稿時刻が直近1〜3時間の窓に入らない限り、特別な対策なしで自動的に
対象外になる。そのため文字列マッチのハックはここでは意図的に移植していない。
唯一の例外は「直近1時間以内に新しくピン留めされたコメント」だが、これは
実質的に新規コメントそのものなのでカウントされて問題ない。

【集計をSQL側に寄せている理由(2026-07-29)】
以前は「スレッドIDを全部引く → 200件ずつ IN() に詰めて返信を引く → Python側で
1行ずつ数える」という組み立てだった。これはスレッドIDと返信の全行をHTTPで
運んでから捨てているだけで、チャンクごとに別リクエストを往復するぶん遅く、
コメントが伸びるほど転送量も比例して増えていた。

親子の対応付けは JOIN で、件数は GROUP BY handle で、いずれもSQL側で完結できる。
書き換え後は3クエリ固定で、返るのは「ハンドルごとの件数」だけ(数百行程度)。
EXPLAIN QUERY PLAN で3本とも両側が idx_parent_published の SEARCH になることを
確認済み(SCANなし)。
"""


def _grouped_counts(turso_client, sql: str, args: list) -> tuple[dict[str, int], int]:
    """GROUP BY handle の結果を ({handle: count}, 総件数) に変換する。

    handle が NULL の行も総件数には含めるが、アカウント別の内訳には入れない
    (旧実装の `if handle:` と同じ扱い)。
    """
    account: dict[str, int] = {}
    total = 0
    for row in turso_client.query(sql, args):
        c = row["c"]
        total += c
        handle = row["handle"]
        if handle:
            account[handle] = account.get(handle, 0) + c
    return account, total


_FRESH_THREADS_SQL = """
SELECT handle, COUNT(*) AS c FROM comments
WHERE parent_id IS NULL AND published_at >= ? AND is_deleted = 0
GROUP BY handle
"""

# 直近スレッドの返信は投稿時刻を問わず全部(モジュールdocstring参照)。
_FRESH_REPLIES_SQL = """
SELECT r.handle AS handle, COUNT(*) AS c
FROM comments r JOIN comments t ON r.parent_id = t.comment_id
WHERE t.parent_id IS NULL AND t.published_at >= ? AND t.is_deleted = 0
  AND r.is_deleted = 0
GROUP BY r.handle
"""

# hours〜extended_hours 時間前のスレッド: スレッド自体は対象外、
# 直近 hours 時間以内の返信のみカウント。
_OLDER_REPLIES_SQL = """
SELECT r.handle AS handle, COUNT(*) AS c
FROM comments r JOIN comments t ON r.parent_id = t.comment_id
WHERE t.parent_id IS NULL AND t.published_at >= ? AND t.published_at < ?
  AND t.is_deleted = 0
  AND r.is_deleted = 0 AND r.published_at >= ?
GROUP BY r.handle
"""


def get_recent_comment_counts(turso_client, now_epoch: int, hours: int = 1, extended_hours: int = 3) -> dict:
    """戻り値: {"account": {handle: count}, "thread_count": int, "count": int}
    (supabase_record/comment_count.py の get_recent_comment_counts と同じ形)
    """
    cutoff_recent = now_epoch - hours * 3600
    cutoff_extended = now_epoch - extended_hours * 3600

    account: dict[str, int] = {}

    def _merge(part: dict[str, int]) -> None:
        for handle, c in part.items():
            account[handle] = account.get(handle, 0) + c

    # 固定コメント対策の文字列マッチは意図的に移植していない
    # (published_at で絞り込む時点で固定コメントは自然に対象外になるため。
    #  モジュールdocstring参照)。
    thread_acc, thread_count = _grouped_counts(turso_client, _FRESH_THREADS_SQL, [cutoff_recent])
    _merge(thread_acc)

    fresh_reply_acc, fresh_reply_count = _grouped_counts(
        turso_client, _FRESH_REPLIES_SQL, [cutoff_recent]
    )
    _merge(fresh_reply_acc)

    older_reply_acc, older_reply_count = _grouped_counts(
        turso_client, _OLDER_REPLIES_SQL, [cutoff_extended, cutoff_recent, cutoff_recent]
    )
    _merge(older_reply_acc)

    count = thread_count + fresh_reply_count + older_reply_count
    return {"account": account, "thread_count": thread_count, "count": count}
