import argparse
import json
import os
import sys
from typing import Dict, Optional

import spotipy
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOAuth


def to_text(value: object, default: str = "") -> str:
    if isinstance(value, str):
        return value
    return default


def to_int(value: object, default: int = 0) -> int:
    if isinstance(value, int):
        return value
    return default


def chunked(items: list[str], size: int) -> list[list[str]]:
    output: list[list[str]] = []
    index = 0
    while index < len(items):
        output.append(items[index : index + size])
        index += size
    return output


def is_supported_playlist_uri(uri: str) -> bool:
    return uri.startswith("spotify:track:") or uri.startswith("spotify:episode:")


def uri_to_id(uri: str, prefix: str) -> str:
    expected = f"spotify:{prefix}:"
    if not uri.startswith(expected):
        return ""
    return uri[len(expected) :]


def read_uri_list(raw: object, prefix: str) -> list[str]:
    if not isinstance(raw, list):
        return []
    output: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        uri = item.strip()
        if not uri.startswith(f"spotify:{prefix}:"):
            continue
        if uri in seen:
            continue
        seen.add(uri)
        output.append(uri)
    return output


def load_env_file(path: str) -> Dict[str, str]:
    output: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as file:
            for raw in file:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                clean_key = key.strip()
                clean_value = value.strip().strip('"').strip("'")
                if clean_key:
                    output[clean_key] = clean_value
    except FileNotFoundError:
        return output
    return output


def read_credential(
    value: Optional[str], env_name: str, env_file_values: Dict[str, str]
) -> str:
    if value:
        return value.strip()
    file_value = env_file_values.get(env_name, "").strip()
    if file_value:
        return file_value
    env_value = os.getenv(env_name, "").strip()
    return env_value


def convert_spotify_exception(error: SpotifyException) -> RuntimeError | None:
    message = str(error)
    premium_error = "Active premium subscription required for the owner of the app"
    allowlist_error = "user may not be registered"

    if premium_error in message:
        return RuntimeError(
            " ".join(
                [
                    "Spotify 开发模式现在要求 App 所有者账号具备有效的 Premium 订阅。",
                    "请确认创建这个 Client ID 的账号本身就是 Premium；",
                    "如果你刚开通或恢复订阅，请等待几小时后再重试。",
                    "这个限制无法通过脚本绕过。",
                ]
            )
        )

    if allowlist_error in message:
        return RuntimeError(
            " ".join(
                [
                    "Spotify 拒绝了请求，当前账号可能还没加入测试用户。",
                    "请检查 Spotify Developer Dashboard 的 Users and Access，",
                    "并确认当前登录账号已被加入 allowlist。",
                ]
            )
        )

    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import Spotify backup with dedupe and resume"
    )
    parser.add_argument("--client-id", default=None)
    parser.add_argument("--client-secret", default=None)
    parser.add_argument("--redirect-uri", default="https://example.com/callback")
    parser.add_argument("--input", default="spotify_backup.json")
    parser.add_argument("--cache-path", default=".cache-import-spotify")
    parser.add_argument("--checkpoint", default="spotify_import_checkpoint.json")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--show-dialog", action="store_true")
    return parser.parse_args()


def read_backup(path: str) -> dict[str, object]:
    with open(path, "r", encoding="utf-8") as file:
        raw = json.load(file)

    if not isinstance(raw, dict):
        raise RuntimeError("Invalid backup file: root must be object")

    playlists_raw = raw.get("playlists")
    playlists_src = playlists_raw if isinstance(playlists_raw, list) else []
    playlists: list[dict[str, object]] = []

    for item in playlists_src:
        if not isinstance(item, dict):
            continue
        name = to_text(item.get("name"), "Untitled Playlist")
        source_id = to_text(item.get("id"))
        tracks_raw = item.get("tracks")
        track_rows = tracks_raw if isinstance(tracks_raw, list) else []

        uris: list[str] = []
        seen: set[str] = set()
        for track in track_rows:
            if not isinstance(track, dict):
                continue
            uri = to_text(track.get("uri"))
            if not is_supported_playlist_uri(uri):
                continue
            if uri in seen:
                continue
            seen.add(uri)
            uris.append(uri)

        playlists.append({"name": name, "source_id": source_id, "uris": uris})

    liked_tracks = read_uri_list(raw.get("liked_tracks"), "track")
    saved_episodes = read_uri_list(raw.get("saved_episodes"), "episode")

    return {
        "playlists": playlists,
        "liked_tracks": liked_tracks,
        "saved_episodes": saved_episodes,
    }


def load_checkpoint(path: str) -> dict[str, object]:
    try:
        with open(path, "r", encoding="utf-8") as file:
            raw = json.load(file)
            if isinstance(raw, dict):
                return raw
    except FileNotFoundError:
        return {"completed": {}, "library": {}}
    except json.JSONDecodeError:
        return {"completed": {}, "library": {}}
    return {"completed": {}, "library": {}}


def save_checkpoint(path: str, data: dict[str, object]) -> None:
    with open(path, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def make_checkpoint_key(index: int, playlist_name: str, source_id: str) -> str:
    if source_id:
        return f"{index}:{source_id}:{playlist_name}"
    return f"{index}:noid:{playlist_name}"


def list_my_playlists(sp: spotipy.Spotify) -> list[dict[str, object]]:
    page = sp.current_user_playlists(limit=50)
    output: list[dict[str, object]] = []
    while page:
        items = page.get("items") if isinstance(page, dict) else None
        rows = items if isinstance(items, list) else []
        for row in rows:
            if isinstance(row, dict):
                output.append(row)
        next_url = page.get("next") if isinstance(page, dict) else None
        if next_url:
            next_page = sp.next(page)
            if not next_page:
                break
            page = next_page
        else:
            break
    return output


def list_playlist_item_uris(sp: spotipy.Spotify, playlist_id: str) -> set[str]:
    page = sp.playlist_items(
        playlist_id,
        fields="items(track(uri)),next",
        additional_types=("track", "episode"),
        limit=100,
    )
    uris: set[str] = set()
    while page:
        items = page.get("items") if isinstance(page, dict) else None
        rows = items if isinstance(items, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            track = row.get("track")
            if not isinstance(track, dict):
                continue
            uri = to_text(track.get("uri"))
            if is_supported_playlist_uri(uri):
                uris.add(uri)
        next_url = page.get("next") if isinstance(page, dict) else None
        if next_url:
            next_page = sp.next(page)
            if not next_page:
                break
            page = next_page
        else:
            break
    return uris


def choose_or_create_playlist(
    sp: spotipy.Spotify,
    user_id: str,
    target_name: str,
    current_playlists: list[dict[str, object]],
) -> str:
    for playlist in current_playlists:
        if not isinstance(playlist, dict):
            continue
        name = to_text(playlist.get("name"))
        if name != target_name:
            continue
        owner = playlist.get("owner")
        if isinstance(owner, dict):
            owner_id = to_text(owner.get("id"))
            if owner_id and owner_id != user_id:
                continue
        playlist_id = to_text(playlist.get("id"))
        if playlist_id:
            return playlist_id

    created = sp.user_playlist_create(
        user=user_id,
        name=target_name,
        public=False,
        collaborative=False,
    )
    if not isinstance(created, dict):
        raise RuntimeError(f"Cannot create playlist: {target_name}")
    playlist_id = to_text(created.get("id"))
    if not playlist_id:
        raise RuntimeError(f"Cannot create playlist: {target_name}")
    current_playlists.append(created)
    return playlist_id


def list_saved_track_ids(sp: spotipy.Spotify) -> set[str]:
    page = sp.current_user_saved_tracks(limit=50)
    output: set[str] = set()
    while page:
        items = page.get("items") if isinstance(page, dict) else None
        rows = items if isinstance(items, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            track = row.get("track")
            if not isinstance(track, dict):
                continue
            track_id = to_text(track.get("id"))
            if track_id:
                output.add(track_id)
        next_url = page.get("next") if isinstance(page, dict) else None
        if next_url:
            next_page = sp.next(page)
            if not next_page:
                break
            page = next_page
        else:
            break
    return output


def list_saved_episode_ids(sp: spotipy.Spotify) -> set[str]:
    page = sp.current_user_saved_episodes(limit=50)
    output: set[str] = set()
    while page:
        items = page.get("items") if isinstance(page, dict) else None
        rows = items if isinstance(items, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            episode = row.get("episode")
            if not isinstance(episode, dict):
                continue
            episode_id = to_text(episode.get("id"))
            if episode_id:
                output.add(episode_id)
        next_url = page.get("next") if isinstance(page, dict) else None
        if next_url:
            next_page = sp.next(page)
            if not next_page:
                break
            page = next_page
        else:
            break
    return output


def restore_liked_tracks(
    sp: spotipy.Spotify,
    liked_track_uris: list[str],
    checkpoint: dict[str, object],
    checkpoint_path: str,
) -> tuple[int, int]:
    library_raw = checkpoint.get("library")
    library = library_raw if isinstance(library_raw, dict) else {}

    if library.get("liked_tracks_done") is True:
        return (
            to_int(library.get("liked_tracks_added")),
            to_int(library.get("liked_tracks_skipped")),
        )

    existing = list_saved_track_ids(sp)
    source_ids: list[str] = []
    seen: set[str] = set()
    for uri in liked_track_uris:
        track_id = uri_to_id(uri, "track")
        if not track_id:
            continue
        if track_id in seen:
            continue
        seen.add(track_id)
        source_ids.append(track_id)

    to_add = [item for item in source_ids if item not in existing]
    skipped = len(source_ids) - len(to_add)
    for batch in chunked(to_add, 50):
        sp.current_user_saved_tracks_add(tracks=batch)

    library["liked_tracks_done"] = True
    library["liked_tracks_added"] = len(to_add)
    library["liked_tracks_skipped"] = skipped
    checkpoint["library"] = library
    save_checkpoint(checkpoint_path, checkpoint)
    return (len(to_add), skipped)


def restore_saved_episodes(
    sp: spotipy.Spotify,
    saved_episode_uris: list[str],
    checkpoint: dict[str, object],
    checkpoint_path: str,
) -> tuple[int, int]:
    library_raw = checkpoint.get("library")
    library = library_raw if isinstance(library_raw, dict) else {}

    if library.get("saved_episodes_done") is True:
        return (
            to_int(library.get("saved_episodes_added")),
            to_int(library.get("saved_episodes_skipped")),
        )

    existing = list_saved_episode_ids(sp)
    source_ids: list[str] = []
    seen: set[str] = set()
    for uri in saved_episode_uris:
        episode_id = uri_to_id(uri, "episode")
        if not episode_id:
            continue
        if episode_id in seen:
            continue
        seen.add(episode_id)
        source_ids.append(episode_id)

    to_add = [item for item in source_ids if item not in existing]
    skipped = len(source_ids) - len(to_add)
    for batch in chunked(to_add, 50):
        sp.current_user_saved_episodes_add(episodes=batch)

    library["saved_episodes_done"] = True
    library["saved_episodes_added"] = len(to_add)
    library["saved_episodes_skipped"] = skipped
    checkpoint["library"] = library
    save_checkpoint(checkpoint_path, checkpoint)
    return (len(to_add), skipped)


def main() -> None:
    args = parse_args()
    env_file_values = load_env_file(args.env_file)
    client_id = read_credential(args.client_id, "SPOTIFY_CLIENT_ID", env_file_values)
    client_secret = read_credential(
        args.client_secret,
        "SPOTIFY_CLIENT_SECRET",
        env_file_values,
    )

    if not client_id or not client_secret:
        raise RuntimeError(
            "Missing credentials. Put SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in .env, or pass --client-id/--client-secret."
        )

    scope = (
        "playlist-read-private playlist-read-collaborative "
        "playlist-modify-private playlist-modify-public "
        "user-library-read user-library-modify user-read-email"
    )

    print("Connecting to Spotify...")
    sp = spotipy.Spotify(
        auth_manager=SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=args.redirect_uri,
            scope=scope,
            cache_path=args.cache_path,
            show_dialog=args.show_dialog,
        )
    )

    try:
        user = sp.current_user()

        if not user:
            raise RuntimeError("Unable to fetch current user")

        user_id = to_text(user.get("id"))
        if not user_id:
            raise RuntimeError("Unable to fetch user id")
        print(
            f"Import account: {to_text(user.get('display_name'), 'Unknown')} ({user_id})"
        )

        backup = read_backup(args.input)
        playlists_raw = backup.get("playlists")
        playlists = playlists_raw if isinstance(playlists_raw, list) else []
        liked_tracks = read_uri_list(backup.get("liked_tracks"), "track")
        saved_episodes = read_uri_list(backup.get("saved_episodes"), "episode")

        print(f"Playlists in backup: {len(playlists)}")
        print(f"Liked tracks in backup: {len(liked_tracks)}")
        print(f"Saved episodes in backup: {len(saved_episodes)}")

        checkpoint = load_checkpoint(args.checkpoint)
        completed_raw = checkpoint.get("completed")
        completed = completed_raw if isinstance(completed_raw, dict) else {}

        current_playlists = list_my_playlists(sp)
        total_added = 0
        total_skipped = 0

        for index, playlist_row in enumerate(playlists):
            if not isinstance(playlist_row, dict):
                continue
            name = to_text(playlist_row.get("name"), "Untitled Playlist")
            source_id = to_text(playlist_row.get("source_id"))
            key = make_checkpoint_key(index, name, source_id)

            if key in completed:
                done = completed.get(key)
                done_added = 0
                done_skipped = 0
                if isinstance(done, dict):
                    done_added = to_int(done.get("added"))
                    done_skipped = to_int(done.get("skipped"))
                total_added += done_added
                total_skipped += done_skipped
                print(
                    f"Resume skip: {name} | added {done_added} | skipped {done_skipped}"
                )
                continue

            uris_raw = playlist_row.get("uris")
            source_uris = uris_raw if isinstance(uris_raw, list) else []
            item_uris = [
                uri
                for uri in source_uris
                if isinstance(uri, str) and is_supported_playlist_uri(uri)
            ]

            if not item_uris:
                completed[key] = {"added": 0, "skipped": 0}
                checkpoint["completed"] = completed
                save_checkpoint(args.checkpoint, checkpoint)
                print(f"Skip empty playlist: {name}")
                continue

            playlist_id = choose_or_create_playlist(
                sp, user_id, name, current_playlists
            )
            existing = list_playlist_item_uris(sp, playlist_id)
            to_add = [uri for uri in item_uris if uri not in existing]
            skipped = len(item_uris) - len(to_add)

            if to_add:
                for batch in chunked(to_add, 100):
                    sp.playlist_add_items(playlist_id, batch)

            added = len(to_add)
            total_added += added
            total_skipped += skipped
            completed[key] = {"added": added, "skipped": skipped}
            checkpoint["completed"] = completed
            save_checkpoint(args.checkpoint, checkpoint)
            print(f"Synced playlist: {name} | added {added} | skipped {skipped}")

        liked_added, liked_skipped = restore_liked_tracks(
            sp, liked_tracks, checkpoint, args.checkpoint
        )
        epi_added, epi_skipped = restore_saved_episodes(
            sp, saved_episodes, checkpoint, args.checkpoint
        )

        print("Done")
        print(f"Playlist items added: {total_added}")
        print(f"Playlist items skipped: {total_skipped}")
        print(f"Liked tracks added: {liked_added} | skipped: {liked_skipped}")
        print(f"Saved episodes added: {epi_added} | skipped: {epi_skipped}")
    except SpotifyException as error:
        friendly_error = convert_spotify_exception(error)
        if friendly_error is not None:
            raise friendly_error from error
        raise


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
