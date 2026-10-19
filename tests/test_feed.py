from feed import merge_feed_results


def test_merge_feed_results_interleaves_searches_round_robin():
    merged = merge_feed_results(
        ["music", "science"],
        {
            "music": [{"id": "m1"}, {"id": "m2"}],
            "science": [{"id": "s1"}, {"id": "s2"}],
        },
    )

    assert [video["id"] for video in merged] == ["m1", "s1", "m2", "s2"]


def test_merge_feed_results_deduplicates_video_ids_across_searches():
    merged = merge_feed_results(
        ["music", "science"],
        {
            "music": [{"id": "same"}, {"id": "m2"}],
            "science": [{"id": "same"}, {"id": "s2"}],
        },
    )

    assert [video["id"] for video in merged] == ["same", "m2", "s2"]


def test_merge_feed_results_drops_watched_videos_and_missing_ids():
    merged = merge_feed_results(
        ["music", "science"],
        {
            "music": [{"id": "watched"}, {"title": "No ID"}, {"id": "m2"}],
            "science": [{"id": "s1"}, {"id": "watched"}],
        },
        watched_ids={"watched"},
    )

    assert [video["id"] for video in merged] == ["s1", "m2"]
