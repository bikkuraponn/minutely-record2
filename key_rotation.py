"""YouTube API キーのローテーション。record_minutely.py から使う。

record_minutely.py 本体はモジュールレベルで即実行されるトップレベルスクリプト
(importするだけで実際にTurso接続・YouTube API呼び出しまで走ってしまう)なので、
テスト容易性のためこのロジックだけ副作用の無い別ファイルに分離している
(comment_velocity.py/hourly_archive.py と同じ構成)。

レート制限(短時間の一時的な制限)とクォータ枯渇(太平洋時間の日次リセットまで
回復しない)を区別する(2026-07-28、comment_sync/sync.py と同じ理由で分離)。
分けずに両方を即座にローテーションすると、単なる一時的な混雑で「このキーは
まだ十分予算が残っているのに次のキーへ切り替える」誤判定を招き、複数
プロジェクトを跨いで運用している意味が薄れる。
"""
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from googleapiclient.errors import HttpError

_PACIFIC = ZoneInfo("America/Los_Angeles")

_CREATE_KEY_STATE_SQL = """
CREATE TABLE IF NOT EXISTS youtube_key_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  key_idx INTEGER NOT NULL,
  pt_date TEXT NOT NULL,
  updated_at INTEGER NOT NULL
)
"""

_UPSERT_KEY_STATE_SQL = """
INSERT INTO youtube_key_state (id, key_idx, pt_date, updated_at) VALUES (1, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
  key_idx = excluded.key_idx,
  pt_date = excluded.pt_date,
  updated_at = excluded.updated_at
"""

MAX_RATE_LIMIT_RETRIES = 3  # comment_sync(5回)より少なめ。wait_until_next_minute()で
                              # 1分ちょうどに同期する設計のため、バックオフで待ちすぎると
                              # 次の分のトリガーとキューイングして遅延が蓄積するのを避ける。
RATE_LIMIT_BACKOFF_BASE_SEC = 2  # 2, 4, 8 秒と指数バックオフ(最大14秒/呼び出し)

_API_KEYS = [k for k in [
    os.getenv("YOUTUBE_API_KEY"),
    os.getenv("YOUTUBE_API_KEY2"),
    os.getenv("YOUTUBE_API_KEY3"),
] if k]
_key_idx = 0
_turso = None


def _pt_today() -> str:
    """YouTube APIの日次クォータは太平洋時間の0:00にリセットされるので、
    「どのキーまで使い切ったか」の有効期限もその日付で持つ。"""
    return datetime.now(_PACIFIC).strftime("%Y-%m-%d")


def init_state(turso_client) -> None:
    """前回の実行がどのキーまでローテーションしていたかを引き継ぐ。

    record_minutely.py は常駐せず毎分まっさらなプロセスとして起動するため、
    モジュール変数の _key_idx は毎回0に戻る。つまりキー1をその日のうちに
    使い切っていても、以降1440回すべての実行が「まずキー1で叩いて失敗し、
    それからローテーションする」を繰り返していた。1回の実行あたり
    (枯渇済みキーの本数)ぶんの無駄なリクエスト往復が毎分発生する。

    太平洋時間の日付が変わっていれば全キーのクォータが戻っているので、
    保存された位置は捨ててキー1から再開する。

    状態の読み書きに失敗しても記録本体を止めてはいけないので、例外は
    握りつぶしてメモリ上の既定値(キー1から)にフォールバックする。
    """
    global _turso, _key_idx
    try:
        turso_client.execute(_CREATE_KEY_STATE_SQL)
        rows = turso_client.query(
            "SELECT key_idx, pt_date FROM youtube_key_state WHERE id = 1"
        )
        _turso = turso_client
        if rows and rows[0]["pt_date"] == _pt_today():
            idx = int(rows[0]["key_idx"])
            if 0 <= idx < len(_API_KEYS):
                _key_idx = idx
                if idx:
                    print(f"前回の続きからキー {idx + 1}/{len(_API_KEYS)} で開始")
    except Exception as e:
        print(f"キー状態の読み込みに失敗（キー1から開始）: {e}", flush=True)


def _persist_key_idx() -> None:
    if _turso is None:
        return
    try:
        _turso.execute(_UPSERT_KEY_STATE_SQL, [_key_idx, _pt_today(), int(time.time())])
    except Exception as e:
        print(f"キー状態の保存に失敗（次回はキー1から）: {e}", flush=True)


def is_daily_quota_error(e: HttpError) -> bool:
    """1日のプロジェクトクォータを使い切った(太平洋時間の日次リセットまで回復しない)。"""
    err = str(e).lower()
    return e.resp.status in (403, 429) and any(s in err for s in [
        "quotaexceeded", "dailylimitexceeded", "userdailylimitexceeded",
    ])


def is_rate_limit_error(e: HttpError) -> bool:
    """短時間(概ね100秒)のレート制限。日次クォータの枯渇ではなく、少し待てば
    同じキーのまま回復する。userRateLimitExceeded/rateLimitExceeded がこれに該当。"""
    err = str(e).lower()
    return e.resp.status in (403, 429) and "ratelimitexceeded" in err


def fetch_with_rotation(fn, *args):
    """レート制限は同じキーのままバックオフして再試行し、日次クォータ枯渇
    (またはバックオフ上限に達したレート制限)は次のキーへローテーションする。
    全キーを使い切ったら今回の記録をスキップして終了する(sys.exit(0))。
    """
    global _key_idx
    for _ in range(len(_API_KEYS)):
        rate_limit_retries = 0
        while True:
            try:
                return fn(_API_KEYS[_key_idx], *args)
            except HttpError as e:
                if is_rate_limit_error(e) and rate_limit_retries < MAX_RATE_LIMIT_RETRIES:
                    wait = RATE_LIMIT_BACKOFF_BASE_SEC * (2 ** rate_limit_retries)
                    rate_limit_retries += 1
                    print(f"レート制限、{wait}秒待って同じキーでリトライ"
                          f"({rate_limit_retries}/{MAX_RATE_LIMIT_RETRIES})")
                    time.sleep(wait)
                    continue
                if is_daily_quota_error(e) or is_rate_limit_error(e):
                    daily = is_daily_quota_error(e)
                    _key_idx = (_key_idx + 1) % len(_API_KEYS)
                    reason = "クォータ枯渇" if daily else "レート制限がバックオフ上限に到達"
                    print(f"{reason} → キー {_key_idx + 1}/{len(_API_KEYS)} にローテーション")
                    # 日次クォータの枯渇だけを次回以降へ引き継ぐ。レート制限は
                    # 一時的なもので、そのキーの1日の予算はまだ残っているため、
                    # ここで保存すると残り予算を無駄に切り捨てることになる。
                    if daily:
                        _persist_key_idx()
                    break
                raise
    print("ERROR: 全キーのクォータが枯渇。今回の記録をスキップ")
    sys.exit(0)
