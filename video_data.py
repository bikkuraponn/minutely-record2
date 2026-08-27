from googleapiclient.discovery import build


def get_video_stats(api_key, video_id):
    youtube = build('youtube', 'v3', developerKey=api_key)
    video_response = youtube.videos().list(
        part='statistics',
        id=video_id
    ).execute()
    items = video_response.get('items') or []
    if not items:
        raise RuntimeError(
            f"videos.list returned no items for video_id={video_id!r} "
            "(video deleted/private, or a transient API glitch)"
        )
    stats = items[0]['statistics']
    return {
        'comment_count': int(stats.get('commentCount', 0)),
        'view_count': int(stats.get('viewCount', 0)),
        'like_count': int(stats.get('likeCount', 0))
    }
