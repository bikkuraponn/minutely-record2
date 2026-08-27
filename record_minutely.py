import os
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
import dotenv
from zoneinfo import ZoneInfo
import video_data
import youtube_channel_stats
import hourly_archive
from key_rotation import fetch_with_rotation, init_state as init_key_state
from turso_client import TursoClient

def wait_until_next_minute():
    now = datetime.now(timezone.utc)
    next_minute = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    wait_seconds = (next_minute - now).total_seconds()
    if 0 < wait_seconds:
        time.sleep(wait_seconds)

def parse_timestamp():
    return datetime.now(ZoneInfo("Asia/Tokyo")).isoformat()

dotenv.load_dotenv(Path(__file__).parent.parent / "flaskr" / ".env")
VIDEO_ID = os.getenv("VIDEO_ID")
CHANNEL_ID = os.getenv("CHANNEL_ID")
SECOND_CHANNEL_ID = os.getenv("SECOND_CHANNEL_ID")
# 第2チャンネル用カラムの接頭辞。このリポジトリは公開なので、実際の接頭辞の
# 文字列をソースに直書きしない(値は.env/GitHub Secretsだけが持つ)。
SECOND_TOPIC_PREFIX = os.getenv("SECOND_TOPIC_PREFIX")

_CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS stats_minutely (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    comment_count INTEGER,
    view_count INTEGER,
    like_count INTEGER,
    channel_comment_count INTEGER,
    channel_view_count INTEGER,
    channel_like_count INTEGER,
    {SECOND_TOPIC_PREFIX}_view_count INTEGER,
    {SECOND_TOPIC_PREFIX}_like_count INTEGER,
    {SECOND_TOPIC_PREFIX}_comment_count INTEGER
)
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_stats_minutely_created ON stats_minutely(created_at)
"""

_INSERT = f"""
INSERT INTO stats_minutely (
    created_at, comment_count, view_count, like_count,
    channel_comment_count, channel_view_count, channel_like_count,
    {SECOND_TOPIC_PREFIX}_view_count, {SECOND_TOPIC_PREFIX}_like_count, {SECOND_TOPIC_PREFIX}_comment_count
) VALUES (?,?,?,?,?,?,?,?,?,?)
"""

turso = TursoClient(os.getenv("TURSO_URL"), os.getenv("TURSO_AUTH_TOKEN"))
turso.execute(_CREATE_TABLE)
turso.execute(_CREATE_INDEX)

# 前回の実行がどのキーまで使い切っていたかを引き継ぐ（毎分プロセスが
# 作り直されるため、これが無いと枯渇済みキーへの無駄な呼び出しが毎分出る）。
init_key_state(turso)

wait_until_next_minute()
now_utc = datetime.now(timezone.utc)

video_stats = fetch_with_rotation(video_data.get_video_stats, VIDEO_ID)
channel_stats = fetch_with_rotation(youtube_channel_stats.get_channel_statistics, CHANNEL_ID, turso)
second_channel_stats = fetch_with_rotation(youtube_channel_stats.get_channel_statistics, SECOND_CHANNEL_ID, turso)

# get_channel_statistics()はchannels.listがitemsを返さない場合(削除/非公開化、
# または一時的なAPI不具合)にNoneを返す設計。そのまま添字アクセスすると
# このTurso書き込みより前で意味不明な TypeError になるため、原因を明示して
# ここで止める(この分のstats_minutely行は書かれない=挙動自体は従来通り)。
if channel_stats is None:
    raise RuntimeError("get_channel_statistics returned None for the main channel (channels.list items empty)")
if second_channel_stats is None:
    raise RuntimeError("get_channel_statistics returned None for the second tracked channel (channels.list items empty)")

# 毎分のTurso書き込みは、この後の毎時Supabaseアーカイブの成否に一切影響
# されてはいけない(karotter_bot分割の教訓と同じ: 後段の失敗が前段を止めない)。
# そのため必ずこのTurso書き込みを先に完了させる。
turso.execute(_INSERT, [
    parse_timestamp(),
    video_stats["comment_count"],
    video_stats["view_count"],
    video_stats["like_count"],
    channel_stats["total_comments"],
    channel_stats["total_views"],
    channel_stats["total_likes"],
    second_channel_stats["total_views"],
    second_channel_stats["total_likes"],
    second_channel_stats["total_comments"],
])

# 毎時のSupabase `stats` アーカイブ(該当する分だけ実際に動く)。
# 失敗してもここで握りつぶし、上のTurso書き込みが確定していることを守る。
try:
    hourly_archive.archive_if_due(turso, now_utc, video_stats, channel_stats, second_channel_stats)
except Exception as e:
    print(f"Supabaseアーカイブ失敗（次回以降の分で再試行される）: {e}", flush=True)
