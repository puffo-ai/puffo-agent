"""Profile, member, space, channel, and avatar cache operations."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from . import disk_cache

PROFILE_CACHE_TTL_SECONDS = 10 * 60
PROFILE_FETCH_CHUNK_SIZE = 50

Log = logging.Logger | logging.LoggerAdapter
ProfileFetcher = Callable[..., Awaitable[tuple[str, str]]]
AvatarFetcher = Callable[[str], Awaitable[None]]
MemberFetcher = Callable[[str], Awaitable[dict[str, str]]]
ChannelWarmer = Callable[[str], Awaitable[None]]
BulkProfileFetcher = Callable[[list[str]], Awaitable[None]]


async def fetch_owner_slug(
    *,
    slug: str,
    owner_cache: dict[str, tuple[str, float]],
    fetch_profile: ProfileFetcher,
) -> str:
    if not slug:
        return ""
    now = time.monotonic()
    cached = owner_cache.get(slug)
    if cached is not None and now - cached[1] < PROFILE_CACHE_TTL_SECONDS:
        return cached[0]
    await fetch_profile(slug, force_refresh=True)
    fresh = owner_cache.get(slug)
    return fresh[0] if fresh else ""


def set_profile(
    *,
    profile_cache: dict[str, tuple[str, str, float]],
    slug: str,
    display_name: str,
    avatar_url: str,
) -> None:
    if slug:
        profile_cache[slug] = (display_name, avatar_url, time.monotonic())


async def fetch_user_profile(
    *,
    slug: str,
    force_refresh: bool,
    profile_cache: dict[str, tuple[str, str, float]],
    owner_cache: dict[str, tuple[str, float]],
    http: Any,
    log: Log,
    fetch_avatar: AvatarFetcher,
) -> tuple[str, str]:
    if not slug:
        return "", ""
    now = time.monotonic()
    if not force_refresh:
        cached = profile_cache.get(slug)
        if cached is not None and now - cached[2] < PROFILE_CACHE_TTL_SECONDS:
            return cached[0], cached[1]

    name, avatar_url, owner_slug = await _request_profile(
        slug=slug,
        http=http,
        log=log,
    )
    profile_cache[slug] = (name, avatar_url, now)
    owner_cache[slug] = (owner_slug, now)
    disk_cache.persist_profile(slug, name, avatar_url)
    if avatar_url:
        asyncio.create_task(fetch_avatar(avatar_url))
    return name, avatar_url


async def _request_profile(
    *,
    slug: str,
    http: Any,
    log: Log,
) -> tuple[str, str, str]:
    try:
        data = await http.get(f"/identities/profiles?slugs={slug}")
    except Exception as exc:
        log.debug("_fetch_user_profile: lookup failed for %s: %s", slug, exc)
        return "", "", ""
    for entry in data.get("profiles") or []:
        if entry.get("slug") == slug:
            return (
                (entry.get("display_name") or "").strip(),
                (entry.get("avatar_url") or "").strip(),
                (entry.get("owner_slug") or "").strip(),
            )
    return "", "", ""


async def warm_member_caches(
    *,
    http: Any,
    log: Log,
    space_name_cache: dict[str, str],
    profile_cache: dict[str, tuple[str, str, float]],
    get_members: MemberFetcher,
    warm_channels: ChannelWarmer,
    fetch_profiles: BulkProfileFetcher,
) -> None:
    started = time.monotonic()
    try:
        spaces_response = await http.get("/spaces")
    except Exception as exc:
        log.debug("warm_member_caches: /spaces failed: %s", exc)
        return
    entries = spaces_response.get("spaces") or []
    space_ids = _cache_space_entries(entries, space_name_cache)
    if not space_ids:
        return

    async def warm_one(space_id: str) -> set[str]:
        members_task = asyncio.create_task(get_members(space_id))
        channels_task = asyncio.create_task(warm_channels(space_id))
        members = await members_task
        await channels_task
        return set(members)

    all_slugs: set[str] = set()
    results = await asyncio.gather(
        *(warm_one(space_id) for space_id in space_ids),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, set):
            all_slugs |= result
    new_slugs = [slug for slug in all_slugs if slug not in profile_cache]
    if new_slugs:
        await fetch_profiles(new_slugs)
    log.info(
        "warm_member_caches: %d spaces, %d members, %d new profiles in %.2fs",
        len(space_ids),
        len(all_slugs),
        len(new_slugs),
        time.monotonic() - started,
    )


def _cache_space_entries(
    entries: list[dict[str, Any]],
    cache: dict[str, str],
) -> list[str]:
    space_ids: list[str] = []
    for entry in entries:
        space_id = entry.get("space_id") or ""
        name = (entry.get("name") or "").strip()
        if not space_id:
            continue
        space_ids.append(space_id)
        if name:
            cache.setdefault(space_id, name)
            disk_cache.persist_space(space_id, name)
    return space_ids


async def warm_channels_for_space(
    *,
    space_id: str,
    http: Any,
    store: Any,
    channel_spaces: dict[str, str],
    channel_names: dict[str, str],
) -> None:
    try:
        response = await http.get(f"/spaces/{space_id}/channels")
    except Exception:
        return
    for channel in response.get("channels", []) or []:
        channel_id = channel.get("channel_id") or ""
        name = (channel.get("name") or "").strip()
        if not channel_id:
            continue
        channel_spaces.setdefault(channel_id, space_id)
        if name:
            channel_names.setdefault(channel_id, name)
            disk_cache.persist_channel(channel_id, name, space_id)
        try:
            await store.mark_channel_space(channel_id, space_id)
        except Exception:
            pass


async def bulk_fetch_profiles(
    *,
    slugs: list[str],
    http: Any,
    profile_cache: dict[str, tuple[str, str, float]],
    fetch_avatar: AvatarFetcher,
) -> None:
    now = time.monotonic()
    for start in range(0, len(slugs), PROFILE_FETCH_CHUNK_SIZE):
        chunk = slugs[start : start + PROFILE_FETCH_CHUNK_SIZE]
        try:
            data = await http.get(
                f"/identities/profiles?slugs={','.join(chunk)}",
            )
        except Exception:
            continue
        for entry in data.get("profiles") or []:
            slug = entry.get("slug") or ""
            if not slug:
                continue
            name = (entry.get("display_name") or "").strip()
            avatar_url = (entry.get("avatar_url") or "").strip()
            profile_cache[slug] = (name, avatar_url, now)
            disk_cache.persist_profile(slug, name, avatar_url)
            if avatar_url:
                asyncio.create_task(fetch_avatar(avatar_url))


async def fetch_and_cache_avatar(
    *,
    avatar_url: str,
    http: Any,
    log: Log,
) -> None:
    if not avatar_url:
        return
    cache_path = disk_cache.avatar_cache_path(avatar_url)
    if cache_path.exists():
        return
    server_url = http.server_url.rstrip("/")
    if not avatar_url.startswith(server_url + "/"):
        return
    try:
        data = await http.get_bytes(avatar_url[len(server_url) :])
    except Exception as exc:
        log.debug("avatar fetch failed for %s: %s", avatar_url, exc)
        return
    disk_cache.write_avatar_bytes(avatar_url, data)


async def get_space_members(
    *,
    space_id: str,
    http: Any,
    cache: dict[str, dict[str, str]],
) -> dict[str, str]:
    if not space_id:
        return {}
    cached = cache.get(space_id)
    if cached is not None:
        return cached
    try:
        response = await http.get(f"/spaces/{space_id}/members")
    except Exception:
        cache[space_id] = {}
        return {}
    members = {
        member["slug"]: member.get("identity_type") or "human"
        for member in response.get("members", [])
        if member.get("slug")
    }
    cache[space_id] = members
    return members


async def resolve_space_name(
    *,
    space_id: str,
    http: Any,
    cache: dict[str, str],
) -> str:
    if not space_id:
        return ""
    if space_id in cache:
        return cache[space_id]
    try:
        data = await http.get("/spaces")
    except Exception:
        cache[space_id] = space_id
        return space_id
    for entry in data.get("spaces") or []:
        current_id = entry.get("space_id")
        if not current_id or current_id in cache:
            continue
        name = (entry.get("name") or "").strip() or current_id
        cache[current_id] = name
        if name != current_id:
            disk_cache.persist_space(current_id, name)
    cache.setdefault(space_id, space_id)
    return cache[space_id]


async def resolve_channel_name(
    *,
    space_id: str,
    channel_id: str,
    http: Any,
    cache: dict[str, str],
) -> str:
    if not channel_id or not space_id:
        return channel_id
    if channel_id in cache:
        return cache[channel_id]
    await _cache_channel_listing(
        space_id=space_id,
        http=http,
        cache=cache,
    )
    if channel_id in cache:
        return cache[channel_id]
    name = await _find_channel_name_in_events(
        space_id=space_id,
        channel_id=channel_id,
        http=http,
    )
    cache[channel_id] = name
    if name and name != channel_id:
        disk_cache.persist_channel(channel_id, name, space_id)
    return name


async def _cache_channel_listing(
    *,
    space_id: str,
    http: Any,
    cache: dict[str, str],
) -> None:
    try:
        data = await http.get(f"/spaces/{space_id}/channels")
    except Exception:
        return
    for entry in data.get("channels") or []:
        channel_id = entry.get("channel_id")
        if not channel_id or channel_id in cache:
            continue
        name = (entry.get("name") or "").strip() or channel_id
        cache[channel_id] = name
        if name != channel_id:
            disk_cache.persist_channel(channel_id, name, space_id)


async def _find_channel_name_in_events(
    *,
    space_id: str,
    channel_id: str,
    http: Any,
) -> str:
    name = channel_id
    cursor: str | None = None
    previous_cursor: str | None = None
    try:
        while True:
            path = f"/spaces/{space_id}/events"
            if cursor:
                path += f"?since={cursor}"
            page = await http.get(path)
            for event in page.get("events") or []:
                if event.get("kind") != "create_channel":
                    continue
                payload = event.get("payload") or {}
                if payload.get("channel_id") == channel_id:
                    name = (payload.get("name") or "").strip() or channel_id
                    break
            if name != channel_id or not page.get("has_more"):
                break
            previous_cursor = cursor
            cursor = page.get("next_cursor")
            if not cursor or cursor == previous_cursor:
                break
    except Exception:
        pass
    return name
