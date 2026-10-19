"""Helpers for combining recommendation results."""


def merge_feed_results(queries, results_by_query, watched_ids=()):
    """Interleave query results, removing duplicate and already-watched videos."""
    seen_ids = set(watched_ids)
    merged = []
    result_lists = [results_by_query.get(query, []) for query in queries]
    for index in range(max((len(items) for items in result_lists), default=0)):
        for items in result_lists:
            if index >= len(items):
                continue
            video = items[index]
            video_id = video.get("id")
            if not video_id or video_id in seen_ids:
                continue
            seen_ids.add(video_id)
            merged.append(video)
    return merged
