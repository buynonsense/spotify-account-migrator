import argparse
import json
import os
from typing import Dict, Optional

import spotipy
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOAuth


def to_text(value: object, default: str = "") -> str:
    if isinstance(value, str):
        return value
    return default


def is_supported_uri(uri: str) -> bool:
    return uri.startswith("spotify:track:") or uri.startswith("spotify:episode:")


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export Spotify playlists and library data"
    )
    parser.add_argument("--client-id", default=None)
    parser.add_argument("--client-secret", default=None)
    parser.add_argument("--redirect-uri", default="https://example.com/callback")
    parser.add_argument("--output", default="spotify_backup.json")
    parser.add_argument("--cache-path", default=".cache-export-spotify")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--show-dialog", action="store_true")
    return parser.parse_args()


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

    scope = "playlist-read-private playlist-read-collaborative user-library-read user-read-email"

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
    except SpotifyException as error:
        if error.http_status == 403:
            raise RuntimeError(
                "Spotify denied access. Check Users and Access in Spotify Developer Dashboard and ensure this account is allowed."
            ) from error
        raise

    if not user:
        raise RuntimeError("Unable to fetch current user")

    print(
        f"Logged in as: {to_text(user.get('display_name'), 'Unknown')} ({to_text(user.get('id'), 'Unknown')})"
    )

    backup: dict[str, object] = {
        "meta": {"format_version": 2},
        "playlists": [],
        "liked_tracks": [],
        "saved_episodes": [],
    }
    playlist_list: list[dict[str, object]] = []
    backup["playlists"] = playlist_list
    playlist_episode_count = 0

    playlists = sp.current_user_playlists(limit=50)
    while playlists:
        items = playlists.get("items") if isinstance(playlists, dict) else None
        current_items = items if isinstance(items, list) else []

        for playlist in current_items:
            if not isinstance(playlist, dict):
                continue
            playlist_id = to_text(playlist.get("id"))
            playlist_name = to_text(playlist.get("name"), "Untitled Playlist")
            if not playlist_id:
                continue

            print(f"Exporting playlist: {playlist_name}")
            playlist_data: dict[str, object] = {
                "name": playlist_name,
                "id": playlist_id,
                "tracks": [],
            }

            page = sp.playlist_items(
                playlist_id,
                fields="items(track(uri,name,type,artists(name),show(name))),next",
                additional_types=("track", "episode"),
                limit=100,
            )
            collected: list[object] = []
            while page:
                page_items = page.get("items") if isinstance(page, dict) else None
                if isinstance(page_items, list):
                    collected.extend(page_items)
                next_url = page.get("next") if isinstance(page, dict) else None
                if next_url:
                    next_page = sp.next(page)
                    if not next_page:
                        break
                    page = next_page
                else:
                    break

            track_rows: list[dict[str, str]] = []
            for row in collected:
                if not isinstance(row, dict):
                    continue
                track = row.get("track")
                if not isinstance(track, dict):
                    continue
                uri = to_text(track.get("uri"))
                if not uri or not is_supported_uri(uri):
                    continue
                item_type = to_text(track.get("type"), "track")
                name = to_text(track.get("name"), "Unknown")

                author = "Unknown"
                if item_type == "episode":
                    show_obj = track.get("show")
                    if isinstance(show_obj, dict):
                        author = to_text(show_obj.get("name"), "Unknown Show")
                    playlist_episode_count += 1
                else:
                    artists = track.get("artists")
                    if isinstance(artists, list) and artists:
                        first = artists[0]
                        if isinstance(first, dict):
                            author = to_text(first.get("name"), "Unknown")

                track_rows.append(
                    {
                        "name": name,
                        "artist": author,
                        "uri": uri,
                        "type": item_type,
                    }
                )

            playlist_data["tracks"] = track_rows
            playlist_list.append(playlist_data)

        next_url = playlists.get("next") if isinstance(playlists, dict) else None
        if next_url:
            next_page = sp.next(playlists)
            if not next_page:
                break
            playlists = next_page
        else:
            break

    liked_uris: list[str] = []
    liked_seen: set[str] = set()
    liked_page = sp.current_user_saved_tracks(limit=50)
    while liked_page:
        items = liked_page.get("items") if isinstance(liked_page, dict) else None
        rows = items if isinstance(items, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            track = row.get("track")
            if not isinstance(track, dict):
                continue
            uri = to_text(track.get("uri"))
            if not uri.startswith("spotify:track:"):
                continue
            if uri in liked_seen:
                continue
            liked_seen.add(uri)
            liked_uris.append(uri)
        next_url = liked_page.get("next") if isinstance(liked_page, dict) else None
        if next_url:
            next_page = sp.next(liked_page)
            if not next_page:
                break
            liked_page = next_page
        else:
            break

    episode_uris: list[str] = []
    episode_seen: set[str] = set()
    episode_page = sp.current_user_saved_episodes(limit=50)
    while episode_page:
        items = episode_page.get("items") if isinstance(episode_page, dict) else None
        rows = items if isinstance(items, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            episode = row.get("episode")
            if not isinstance(episode, dict):
                continue
            uri = to_text(episode.get("uri"))
            if not uri.startswith("spotify:episode:"):
                continue
            if uri in episode_seen:
                continue
            episode_seen.add(uri)
            episode_uris.append(uri)
        next_url = episode_page.get("next") if isinstance(episode_page, dict) else None
        if next_url:
            next_page = sp.next(episode_page)
            if not next_page:
                break
            episode_page = next_page
        else:
            break

    backup["liked_tracks"] = liked_uris
    backup["saved_episodes"] = episode_uris

    with open(args.output, "w", encoding="utf-8") as file:
        json.dump(backup, file, ensure_ascii=False, indent=2)

    print("Done")
    print(f"Backup file: {args.output}")
    print(f"Playlists exported: {len(playlist_list)}")
    print(f"Episodes found inside playlists: {playlist_episode_count}")
    print(f"Liked tracks exported: {len(liked_uris)}")
    print(f"Saved episodes exported from library: {len(episode_uris)}")


if __name__ == "__main__":
    main()
