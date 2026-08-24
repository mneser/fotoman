"""SCENE_MATCH pair gating vs HASH_VECTOR-style embedding chaining."""

from fotoman.cluster_scene import (
    SceneCues,
    gps_meters,
    scene_pair_joins,
    scene_params_from_threshold,
)


def _max_range():
    return scene_params_from_threshold(0.86)


def test_hash_vector_would_chain_temples_scene_match_does_not():
    """Same-day temples with high SigLIP scores stay split without a time window."""
    params = _max_range()
    cues = SceneCues(
        embedding=0.91,
        fingerprint=0.88,
        color=0.70,
        dt_seconds=4 * 3600,
        gps_meters=None,  # city geocode
    )
    assert not scene_pair_joins(cues, params)


def test_burst_different_angles_join():
    """Same-subject shots a few seconds apart join even when embeddings are modest."""
    params = _max_range()
    cues = SceneCues(
        embedding=0.62,
        fingerprint=0.90,
        color=0.55,
        dt_seconds=25,
    )
    assert scene_pair_joins(cues, params)


def test_burst_visual_veto():
    """Back-to-back shots of unrelated subjects do not join on time alone."""
    params = _max_range()
    cues = SceneCues(
        embedding=0.30,
        fingerprint=0.70,
        color=0.10,
        dt_seconds=20,
    )
    assert not scene_pair_joins(cues, params)


def test_fingerprint_exact_joins_across_days():
    params = _max_range()
    cues = SceneCues(
        embedding=0.70,
        fingerprint=0.995,
        color=0.40,
        dt_seconds=3 * 86400,
    )
    assert scene_pair_joins(cues, params)


def test_visit_window_needs_a_visual_cue():
    params = _max_range()
    weak = SceneCues(
        embedding=0.50,
        fingerprint=0.88,
        color=0.40,
        dt_seconds=180,
    )
    assert not scene_pair_joins(weak, params)
    ok = SceneCues(
        embedding=0.78,
        fingerprint=0.90,
        color=0.60,
        dt_seconds=180,
    )
    assert scene_pair_joins(ok, params)


def test_homography_joins_in_window():
    params = _max_range()
    cues = SceneCues(
        embedding=0.55,
        fingerprint=0.88,
        color=0.40,
        homography_inliers=16,
        dt_seconds=120,
    )
    assert scene_pair_joins(cues, params)


def test_exif_gps_veto():
    params = _max_range()
    cues = SceneCues(
        embedding=0.88,
        fingerprint=0.90,
        color=0.80,
        dt_seconds=30,
        gps_meters=2500,
    )
    assert not scene_pair_joins(cues, params)


def test_geocode_gps_is_ignored():
    assert gps_meters(11.56, 104.92, 11.57, 104.93, source_a="geocode", source_b="exif") is None
    meters = gps_meters(11.56, 104.92, 11.57, 104.93, source_a="exif", source_b="exif")
    assert meters is not None and meters > 1000


def test_strict_slider_keeps_a_short_window():
    strict = scene_params_from_threshold(0.98)
    assert strict.window_s <= 25
    cues = SceneCues(embedding=0.80, fingerprint=0.90, color=0.80, dt_seconds=60)
    assert not scene_pair_joins(cues, strict)
