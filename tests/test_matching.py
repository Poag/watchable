from __future__ import annotations

from watchable.matching import build_guid_keys, guids_from_stored_keys, is_matchable, parse_guid_key
from watchable.providers.base import EPISODE, MOVIE, EpisodeInfo, GuidSet


def test_build_guid_keys_movie_all_schemes():
    guids = GuidSet(imdb="tt0111161", tmdb="278", tvdb=None)
    keys = build_guid_keys(MOVIE, guids)
    assert set(keys) == {"imdb:tt0111161", "tmdb:278"}


def test_build_guid_keys_movie_empty():
    assert build_guid_keys(MOVIE, GuidSet()) == ()


def test_build_guid_keys_episode():
    show_guids = GuidSet(imdb="tt0903747", tvdb="81189")
    episode = EpisodeInfo(show_guids=show_guids, season_number=1, episode_number=7)
    keys = build_guid_keys(EPISODE, GuidSet(), episode)
    assert set(keys) == {"imdb:tt0903747:S01E07", "tvdb:81189:S01E07"}


def test_is_matchable():
    assert is_matchable(MOVIE, GuidSet(imdb="tt1"))
    assert not is_matchable(MOVIE, GuidSet())
    episode = EpisodeInfo(show_guids=GuidSet(imdb="tt1"), season_number=1, episode_number=1)
    assert is_matchable(EPISODE, GuidSet(), episode)
    empty_episode = EpisodeInfo(show_guids=GuidSet(), season_number=1, episode_number=1)
    assert not is_matchable(EPISODE, GuidSet(), empty_episode)
    assert not is_matchable(EPISODE, GuidSet(), None)


def test_parse_guid_key_movie():
    assert parse_guid_key("imdb:tt0111161") == ("imdb", "tt0111161", None, None)


def test_parse_guid_key_episode():
    assert parse_guid_key("tvdb:81189:S01E07") == ("tvdb", "81189", 1, 7)


def test_guids_from_stored_keys_movie_roundtrip():
    guids, episode = guids_from_stored_keys(["imdb:tt0111161", "tmdb:278"])
    assert guids == GuidSet(imdb="tt0111161", tmdb="278")
    assert episode is None


def test_guids_from_stored_keys_episode_roundtrip():
    guids, episode = guids_from_stored_keys(["imdb:tt0903747:S01E07", "tvdb:81189:S01E07"])
    assert guids == GuidSet()  # movie-level GuidSet is empty for episodes
    assert episode is not None
    assert episode.show_guids == GuidSet(imdb="tt0903747", tvdb="81189")
    assert episode.season_number == 1
    assert episode.episode_number == 7
