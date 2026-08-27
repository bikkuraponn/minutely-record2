"""
毎時のSupabase `stats` アーカイブ。2026-07-28以前は supabase_record/record.py が
別リポジトリ・別スケジュールで独自に YouTube API を叩いて記録していたが、
record_minutely.py が既に毎分同じ動画/チャンネル統計を取得しているため、
その場でSupabaseにも書いてしまうことで supabase_record 側のYouTube API
消費(＝別プロジェクト1本)を丸ごと不要にする。

呼び出し元(record_minutely.py)の毎分のTurso書き込みは、この関数の成否に
一切影響されてはいけない。呼び出し元で try/except すること
(karotter_bot分割の教訓と同じ: 後段の失敗が前段を止めてはいけない)。

再試行方針:
  0〜4分: 毎分試行(バースト)
  5,10,...,55分: 5分おきに試行
  それでも書けなければ次の時間(次のSupabase記録)まで待たず、その時間は諦める
  (`stats` には daily_stats の date のような再計算キーが無く、後から
  UPSERTで埋め直すことができないため。必要なら別途バックフィルで対応する)。

「もう今の時間帯の行が書けているか」は毎回Supabase自体に問い合わせて確認する。
Turso側に別途「書けた」フラグを持つ設計だと、Supabase挿入自体は成功したのに
直後のフラグ書き込みだけ失敗する、というズレにより二重書き込みを招きうる。
Supabase自体への問い合わせは常に真実(実際に行があるか)を見るので、
このズレが原理的に起きない(自己修復的)。
"""
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from supabase import create_client

from comment_velocity import get_recent_comment_counts

# record.py/upload_blob.py と同じ値をそのまま踏襲(このプロジェクトの既存の
# 慣習として、Supabase URLは複数スクリプトにハードコードされている)。
SUPABASE_URL = "https://ywibcfemkofxukrojooj.supabase.co"
TABLE_NAME = "stats"

RETRY_BURST_MINUTES = 5  # 0〜4分(5回)は毎分試行
RETRY_INTERVAL_MIN = 5   # 以降は5分おき(5,10,...,55分)


def should_attempt_this_minute(minute: int) -> bool:
    return minute < RETRY_BURST_MINUTES or minute % RETRY_INTERVAL_MIN == 0


def _hour_range_jst(now_utc: datetime) -> tuple[str, str]:
    """UTC時刻から、そのJST時間帯 [HH:00, HH+1:00) のISO文字列範囲を返す。"""
    now_jst = now_utc.astimezone(ZoneInfo("Asia/Tokyo"))
    hour_start = now_jst.replace(minute=0, second=0, microsecond=0)
    hour_end = hour_start + timedelta(hours=1)
    return hour_start.isoformat(), hour_end.isoformat()


def is_hour_already_archived(supabase_client, now_utc: datetime) -> bool:
    hour_start, hour_end = _hour_range_jst(now_utc)
    res = (
        supabase_client.table(TABLE_NAME)
        .select("id")
        .gte("created_at", hour_start)
        .lt("created_at", hour_end)
        .limit(1)
        .execute()
    )
    return len(res.data) > 0


_DUPLICATE_HOUR_INDEX = "stats_jst_hour_unique"


def _is_duplicate_hour_error(e: Exception) -> bool:
    """「同じJST時間帯の行が既にある」という一意制約違反かどうか。

    Supabase(PostgREST)側は 23505 unique_violation を返す。インデックス名でも
    照合して、無関係な一意制約違反まで「記録済み」として飲み込まないようにする。
    """
    text = str(e)
    return "23505" in text or _DUPLICATE_HOUR_INDEX in text


def _clear_button_stats_cache() -> None:
    flask_base_url = os.getenv("FLASK_BASE_URL")
    api_token = os.getenv("API_TOKEN")
    if not (flask_base_url and api_token):
        return
    try:
        requests.post(
            f"{flask_base_url.rstrip('/')}/api/cache-clear-button-stats",
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=10,
        )
    except Exception as e:
        print(f"  button-statsキャッシュクリア失敗: {e}")


def archive_if_due(
    turso_client, now_utc: datetime, video_stats: dict, channel_stats: dict, second_channel_stats: dict,
) -> bool:
    """毎分呼ばれる想定。試行すべき分でなければ何もせず False を返す。

    戻り値: 実際にSupabaseへ新規挿入したら True、それ以外
    (試行対象外の分/既に記録済み)は False。
    """
    if not should_attempt_this_minute(now_utc.minute):
        return False

    supabase_client = create_client(SUPABASE_URL, os.environ["SUPABASE_SERVICE_ROLE_KEY"])

    if is_hour_already_archived(supabase_client, now_utc):
        return False

    now_epoch = int(now_utc.timestamp())
    comment_counts = get_recent_comment_counts(turso_client, now_epoch)

    # 第2チャンネル用カラムの接頭辞。公開リポジトリなので実際の接頭辞文字列を
    # ソースに直書きしない(record_minutely.py と同じ理由、値は.env/GitHub Secretsのみが持つ)。
    second_topic_prefix = os.environ["SECOND_TOPIC_PREFIX"]
    record = {
        "created_at": now_utc.astimezone(ZoneInfo("Asia/Tokyo")).isoformat(),
        "comment_count": video_stats["comment_count"],
        "view_count": video_stats["view_count"],
        "like_count": video_stats["like_count"],
        "comment_count2": comment_counts["count"],
        "thread_count": comment_counts["thread_count"],
        "comment_account": dict(comment_counts["account"]),
        "comment_account_count": len(comment_counts["account"]),
        "channel_comment_count": channel_stats["total_comments"],
        "channel_view_count": channel_stats["total_views"],
        "channel_like_count": channel_stats["total_likes"],
        f"{second_topic_prefix}_view_count": second_channel_stats["total_views"],
        f"{second_topic_prefix}_like_count": second_channel_stats["total_likes"],
        f"{second_topic_prefix}_comment_count": second_channel_stats["total_comments"],
    }

    # is_hour_already_archived() のチェックとこのINSERTはアトミックではない。
    # GitHub Actions の concurrency は cancel-in-progress: false なので、実行が
    # 詰まって2つの分のrunが接近すると、どちらも「まだ無い」と判定してから
    # 両方が書き込む余地がある(TOCTOU)。`stats` は1時間1行を後から訂正できない
    # 設計なので、重複が入ると恒久的に残ってしまう。
    # そこでDB側に部分ユニークインデックス stats_jst_hour_unique
    # (date_trunc('hour', created_at, 'Asia/Tokyo') に対する UNIQUE) を張り、
    # 競合したINSERTは必ず片方だけが成功するようにしてある。
    # ここで一意制約違反を捕まえるのは「もう片方のrunが書き終えた」という意味なので、
    # 例外にせず「既に記録済み」と同じ扱い(False)で静かに戻る。
    try:
        supabase_client.table(TABLE_NAME).insert(record).execute()
    except Exception as e:
        if _is_duplicate_hour_error(e):
            print(f"  Supabase archive: {record['created_at']} は別runが記録済み"
                  f"（一意制約で重複を回避）", flush=True)
            return False
        raise
    print(f"  Supabase archive: {record['created_at']} を記録", flush=True)
    _clear_button_stats_cache()
    return True
