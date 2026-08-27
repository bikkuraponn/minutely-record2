import json
import time

from googleapiclient.discovery import build
import dotenv
import os

dotenv.load_dotenv()

# 動画IDリスト(=チャンネルの全動画がどれか)は新しい動画が投稿されない限り
# 変わらないのに、以前は毎分 playlistItems.list を全ページ叩き直していた
# (チャンネルあたり ceil(動画数/50) units を、変化の有無に関係なく毎分)。
# これが毎分実行の主要コストだったため、Turso に1日キャッシュする。
# 再生数・高評価・コメント数(videos.list)は毎分変わる値なのでキャッシュしない。
REFRESH_INTERVAL_SEC = 86400  # 1日

_CREATE_CACHE_TABLE = """
CREATE TABLE IF NOT EXISTS channel_video_ids_cache (
  channel_id TEXT PRIMARY KEY,
  video_ids  TEXT NOT NULL,
  updated_at INTEGER NOT NULL
)
"""


def _fetch_video_ids(youtube, channel_id) -> list[str] | None:
    """playlistItems.list で全動画IDを取り直す(API呼び出しあり)。"""
    channel_response = youtube.channels().list(
        part='contentDetails',
        id=channel_id
    ).execute()

    if not channel_response['items']:
        return None

    uploads_playlist_id = channel_response['items'][0]['contentDetails']['relatedPlaylists']['uploads']

    next_page_token = None
    video_ids = []
    while True:
        playlist_response = youtube.playlistItems().list(
            part='contentDetails',
            playlistId=uploads_playlist_id,
            maxResults=50,
            pageToken=next_page_token
        ).execute()
        for item in playlist_response['items']:
            video_ids.append(item['contentDetails']['videoId'])
        next_page_token = playlist_response.get('nextPageToken')
        if not next_page_token:
            break

    return video_ids


def _get_video_ids(youtube, channel_id, turso_client) -> list[str] | None:
    """キャッシュが REFRESH_INTERVAL_SEC 以内なら再利用し、古い/無ければ API から取り直して保存する。"""
    if turso_client is None:
        return _fetch_video_ids(youtube, channel_id)

    turso_client.execute(_CREATE_CACHE_TABLE)
    now = int(time.time())
    rows = turso_client.query(
        "SELECT video_ids, updated_at FROM channel_video_ids_cache WHERE channel_id = ?",
        [channel_id],
    )
    if rows and now - rows[0]["updated_at"] < REFRESH_INTERVAL_SEC:
        return json.loads(rows[0]["video_ids"])

    video_ids = _fetch_video_ids(youtube, channel_id)
    if video_ids is None:
        return None

    turso_client.execute(
        "INSERT INTO channel_video_ids_cache (channel_id, video_ids, updated_at) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(channel_id) DO UPDATE SET video_ids = excluded.video_ids, updated_at = excluded.updated_at",
        [channel_id, json.dumps(video_ids), now],
    )
    return video_ids


def get_channel_statistics(api_key, channel_id, turso_client=None):
    youtube = build('youtube', 'v3', developerKey=api_key)

    video_ids = _get_video_ids(youtube, channel_id, turso_client)
    if video_ids is None:
        return None

    total_views = total_likes = total_comments = video_count = 0
    for i in range(0, len(video_ids), 50):
        video_response = youtube.videos().list(
            part='statistics',
            id=','.join(video_ids[i:i+50])
        ).execute()
        for video in video_response['items']:
            stats = video['statistics']
            total_views += int(stats.get('viewCount', 0))
            total_likes += int(stats.get('likeCount', 0))
            total_comments += int(stats.get('commentCount', 0))
            video_count += 1

    return {
        'channel_id': channel_id,
        'video_count': video_count,
        'total_views': total_views,
        'total_likes': total_likes,
        'total_comments': total_comments,
    }
