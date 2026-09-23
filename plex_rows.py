"""Plex client for per-user "Your Unwatched Requests" rows.

Technique (verified against Plex Media Server 1.43.x):

* One regular collection per (library section, user) holds the user's
  unwatched requests. The collection carries a label ``<prefix><username>``.
* Every *other* shared user's content filters exclude that label
  (``label!=<prefix><username>``). Plex applies share filters to collections,
  so the collection is invisible to everyone but its owner. The label lives on
  the collection only; the movies/shows inside stay visible to everybody.
* The collection is promoted to "Friends' Home" and moved to the top of the
  library's rows, so the owner sees it on their Home screen right under
  Continue Watching.

The server owner cannot be filtered, so the admin sees every collection in
the library's Collections tab. Requires Plex Pass on the admin account and
PMS >= 1.43.2.

This module only talks to Plex; it knows nothing about requests or Tautulli.
The orchestration lives in ``forgotten_movies.sync_plex_rows``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

import requests
import plexapi
from plexapi.exceptions import NotFound
from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer

logger = logging.getLogger("ForgottenMoviesPlexRows")

MIN_PMS_VERSION = (1, 43, 2)
PLEXTV_USERS_URL = "https://plex.tv/api/users/{user_id}"
PLEXTV_DEVICES_URL = "https://plex.tv/devices.xml"
PLEXTV_PINS_URL = "https://plex.tv/api/v2/pins"
PLEX_AUTH_URL = "https://app.plex.tv/auth#?"
PRODUCT_NAME = "Forgotten Movies"


def plex_headers(client_id: str, token: str | None = None) -> dict[str, str]:
    """Headers that identify *this app* as its own Plex device.

    plex.tv binds every token to a device record and rewrites that record from
    the X-Plex-* headers of each request. Always present a stable identity so
    the only record we can ever touch is our own.
    """
    headers = {
        "X-Plex-Client-Identifier": client_id,
        "X-Plex-Product": PRODUCT_NAME,
        "X-Plex-Version": "1.0",
        "X-Plex-Device-Name": PRODUCT_NAME,
        "X-Plex-Device": "Linux",
        "X-Plex-Platform": "Linux",
        "X-Plex-Provides": "controller",
        "Accept": "application/json",
    }
    if token:
        headers["X-Plex-Token"] = token
    return headers


def configure_plexapi_identity(client_id: str) -> None:
    """Make python-plexapi send the same device identity as plex_headers()."""
    plexapi.X_PLEX_IDENTIFIER = client_id
    plexapi.X_PLEX_PRODUCT = PRODUCT_NAME
    plexapi.X_PLEX_DEVICE_NAME = PRODUCT_NAME
    plexapi.X_PLEX_VERSION = "1.0"
    plexapi.X_PLEX_PLATFORM = "Linux"
    plexapi.X_PLEX_DEVICE = "Linux"
    plexapi.X_PLEX_PROVIDES = "controller"
    plexapi.BASE_HEADERS.update({
        "X-Plex-Client-Identifier": client_id,
        "X-Plex-Product": PRODUCT_NAME,
        "X-Plex-Device-Name": PRODUCT_NAME,
        "X-Plex-Version": "1.0",
        "X-Plex-Platform": "Linux",
        "X-Plex-Device": "Linux",
        "X-Plex-Provides": "controller",
    })


def token_device(token: str, client_id: str, timeout: int = 15) -> dict | None:
    """Which plex.tv device does this token belong to? None if not found.

    Sent with the token ONLY: any X-Plex-Product/Provides/Device header on a
    request would make plex.tv rewrite the token's device record, and this
    call runs before we know whose token it is.
    """
    resp = requests.get(PLEXTV_DEVICES_URL, headers={"X-Plex-Token": token}, timeout=timeout)
    resp.raise_for_status()
    import xml.etree.ElementTree as ET
    for node in ET.fromstring(resp.text).iter("Device"):
        if node.get("token") == token:
            return {
                "name": node.get("name"),
                "product": node.get("product"),
                "provides": node.get("provides"),
                "clientIdentifier": node.get("clientIdentifier"),
            }
    return None


def pin_start(client_id: str, forward_url: str | None = None, timeout: int = 15) -> dict:
    """Begin a Plex PIN sign-in. Returns {id, code, auth_url}."""
    resp = requests.post(
        PLEXTV_PINS_URL,
        params={"strong": "true"},
        headers=plex_headers(client_id),
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    from urllib.parse import urlencode
    params = {
        "clientID": client_id,
        "code": data["code"],
        "context[device][product]": PRODUCT_NAME,
        "context[device][deviceName]": PRODUCT_NAME,
        "context[device][platform]": "Linux",
    }
    if forward_url:
        params["forwardUrl"] = forward_url
    return {"id": data["id"], "code": data["code"], "auth_url": PLEX_AUTH_URL + urlencode(params)}


def pin_poll(client_id: str, pin_id: int, timeout: int = 15) -> str | None:
    """Return the auth token once the user has approved the PIN, else None."""
    resp = requests.get(f"{PLEXTV_PINS_URL}/{pin_id}", headers=plex_headers(client_id), timeout=timeout)
    resp.raise_for_status()
    return resp.json().get("authToken") or None


def _version_tuple(text: str) -> tuple[int, ...]:
    parts = []
    for piece in re.split(r"[.\-]", text or ""):
        if piece.isdigit():
            parts.append(int(piece))
        else:
            break
    return tuple(parts)


# ---------------------------------------------------------------------------
# Share filter strings
# ---------------------------------------------------------------------------
# plex.tv stores each user's restrictions as one string per media type, e.g.
# ``contentRating=PG|label!=req_alice,req_bob``. We only ever touch the
# ``label!=`` entries that start with our prefix and leave everything else
# exactly as it was.
def parse_filter(text: str | None) -> list[tuple[str, list[str]]]:
    parsed: list[tuple[str, list[str]]] = []
    for chunk in (text or "").split("|"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        key, _, values = chunk.partition("=")
        vals = [v for v in values.replace("%2C", ",").split(",") if v]
        parsed.append((key, vals))
    return parsed


def build_filter(parts: list[tuple[str, list[str]]]) -> str:
    return "|".join(f"{key}={','.join(vals)}" for key, vals in parts if vals)


def merge_exclusions(existing: str | None, prefix: str, wanted_labels: set[str]) -> str:
    """Return ``existing`` with our prefixed ``label!=`` entries replaced by ``wanted_labels``."""
    parts = parse_filter(existing)
    new_parts: list[tuple[str, list[str]]] = []
    placed = False
    for key, vals in parts:
        if key == "label!":
            kept = [v for v in vals if not v.lower().startswith(prefix.lower())]
            merged = kept + sorted(wanted_labels)
            new_parts.append((key, merged))
            placed = True
        else:
            new_parts.append((key, vals))
    if not placed and wanted_labels:
        new_parts.append(("label!", sorted(wanted_labels)))
    return build_filter(new_parts)


def _filter_has_labels(text: str | None, labels: set[str]) -> bool:
    present = set()
    for key, vals in parse_filter(text):
        if key == "label!":
            present.update(v.lower() for v in vals)
    return all(label.lower() in present for label in labels)


# ---------------------------------------------------------------------------
# Data holders
# ---------------------------------------------------------------------------
@dataclass
class Friend:
    id: int
    username: str
    email: str
    filter_movies: str
    filter_television: str
    section_ids: set[int] = field(default_factory=set)   # server-local library keys shared with them
    title: str = ""                                       # display name shown in Plex
    access_token: str = ""                                # their server-scoped token (local server calls only)


@dataclass
class RowState:
    """What the sync loop needs to know about one user's collection in one library."""
    username: str
    section_id: int
    section_title: str
    section_type: str          # "movie" | "show"
    collection_key: int
    label: str
    item_keys: list[int]


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class PlexRowsClient:
    def __init__(self, url: str, token: str, client_id: str, label_prefix: str = "req_", timeout: int = 30,
                 trusted_token: bool = False):
        self.url = url.rstrip("/")
        self.token = token
        self.client_id = client_id
        self.trusted_token = trusted_token   # obtained via our own PIN sign-in
        self.prefix = label_prefix
        self.timeout = timeout
        configure_plexapi_identity(client_id)
        # Local server only; nothing below talks to plex.tv until the token
        # has passed token_problem() (see account property).
        self.server = PlexServer(self.url, self.token, timeout=timeout)
        self.machine_id = self.server.machineIdentifier
        self._account: MyPlexAccount | None = None
        self._token_checked = False
        self._friends: list[Friend] | None = None
        self._sections: dict[int, object] = {}

    @property
    def account(self) -> MyPlexAccount:
        """plex.tv account object; refuses to exist for a token we can't vouch for."""
        if self._account is None:
            problem = self.token_problem()
            if problem:
                raise RuntimeError(problem)
            self._account = MyPlexAccount(token=self.token, timeout=self.timeout)
        return self._account

    def _plextv_headers(self) -> dict[str, str]:
        problem = self.token_problem()
        if problem:
            raise RuntimeError(problem)
        return plex_headers(self.client_id, self.token)

    def token_problem(self) -> str | None:
        """Why the configured token must not be used against plex.tv, or None.

        plex.tv rewrites the device a token belongs to from our request
        headers. The server's own token (Preferences.xml) is bound to the
        server's device record and is NOT listed in devices.xml, so a token we
        cannot map to a listed device is refused: using it once turned the
        server record into provides=controller and every user lost the server.
        """
        if self._token_checked or self.trusted_token:
            return None
        device = token_device(self.token, self.client_id, timeout=self.timeout)
        if device is None:
            return (
                "This token does not belong to any device listed on your Plex account, so it is "
                "almost certainly the Plex server's own token. Using it would rewrite the server's "
                "plex.tv device record and every user would lose the server. Use 'Sign in with Plex'."
            )
        if device.get("clientIdentifier") == self.machine_id or "server" in (device.get("provides") or ""):
            return "This token belongs to the Plex server itself and must not be used. Use 'Sign in with Plex'."
        if device.get("clientIdentifier") != self.client_id:
            logger.warning(
                "Plex token belongs to device %r (%s), not to this app; consider 'Sign in with Plex'.",
                device.get("name"), device.get("product"),
            )
        self._token_checked = True
        return None

    # -- diagnostics ---------------------------------------------------------
    def describe(self) -> dict:
        version = self.server.version
        info = {
            "server": self.server.friendlyName,
            "version": version,
            "version_ok": _version_tuple(version) >= MIN_PMS_VERSION,
            "plex_pass": bool(getattr(self.account, "subscriptionActive", False)),
            "friends": len(self.friends()),
        }
        return info

    # -- users --------------------------------------------------------------
    def friends(self, refresh: bool = False) -> list[Friend]:
        """Shared users (friends) who have access to this server, with their current filters."""
        if self._friends is not None and not refresh:
            return self._friends
        # plex.tv shared_servers is the authoritative list of who is shared this
        # server, their per-user filters, and which libraries they get.
        resp = requests.get(
            f"https://plex.tv/api/servers/{self.machine_id}/shared_servers",
            headers={**self._plextv_headers(), "Accept": "application/xml"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        import xml.etree.ElementTree as ET
        display_names = {}
        try:
            display_names = {u.id: (u.title or u.username or "") for u in self.account.users()}
        except Exception as exc:
            logger.debug("Could not load display names: %s", exc)
        friends: list[Friend] = []
        for node in ET.fromstring(resp.text).iter("SharedServer"):
            sections = {int(s.get("key")) for s in node.findall("Section") if s.get("shared") == "1" and s.get("key")}
            friends.append(Friend(
                id=int(node.get("userID")),
                username=node.get("username") or "",
                email=node.get("email") or "",
                filter_movies=node.get("filterMovies") or "",
                filter_television=node.get("filterTelevision") or "",
                section_ids=sections,
                title=display_names.get(int(node.get("userID")), "") or (node.get("username") or ""),
                access_token=node.get("accessToken") or "",
            ))
        self._friends = friends
        return friends

    def label_for(self, username: str) -> str:
        return f"{self.prefix}{username}"

    def set_friend_filters(self, friend: Friend, movies: str, television: str) -> None:
        resp = requests.put(
            PLEXTV_USERS_URL.format(user_id=friend.id),
            params={"filterMovies": movies, "filterTelevision": television},
            headers={**self._plextv_headers(), "Accept": "application/xml"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        friend.filter_movies = movies
        friend.filter_television = television

    def ensure_exclusions(self, row_owners: set[str]) -> dict[str, int]:
        """Make every friend exclude every *other* owner's label. Returns counts."""
        labels = {self.label_for(u).lower(): self.label_for(u) for u in row_owners}
        stats = {"checked": 0, "updated": 0, "failed": 0}
        for friend in self.friends():
            stats["checked"] += 1
            own = self.label_for(friend.username).lower()
            wanted = {lbl for key, lbl in labels.items() if key != own}
            movies = merge_exclusions(friend.filter_movies, self.prefix, wanted)
            tv = merge_exclusions(friend.filter_television, self.prefix, wanted)
            if movies == (friend.filter_movies or "") and tv == (friend.filter_television or ""):
                continue
            try:
                self.set_friend_filters(friend, movies, tv)
                stats["updated"] += 1
                logger.info("Updated share filters for %s", friend.username)
            except Exception as exc:
                stats["failed"] += 1
                logger.warning("Failed to update share filters for %s: %s", friend.username, exc)
        return stats

    def exclusions_healthy(self, owner: str) -> bool:
        """True if every other friend currently excludes ``owner``'s label (both media types)."""
        label = {self.label_for(owner)}
        for friend in self.friends():
            if friend.username.lower() == owner.lower():
                continue
            if not (_filter_has_labels(friend.filter_movies, label) and _filter_has_labels(friend.filter_television, label)):
                return False
        return True

    def clear_all_exclusions(self) -> int:
        cleared = 0
        for friend in self.friends():
            movies = merge_exclusions(friend.filter_movies, self.prefix, set())
            tv = merge_exclusions(friend.filter_television, self.prefix, set())
            if movies != (friend.filter_movies or "") or tv != (friend.filter_television or ""):
                self.set_friend_filters(friend, movies, tv)
                cleared += 1
        return cleared

    def watched_by(self, friend: Friend, rating_keys: list[int]) -> dict[int, str]:
        """{ratingKey: lastViewedAt ISO} for the items this friend has watched.

        Asks the local server with the friend's own access token, so it is
        exact per user and unaffected by username changes. Movies count as
        watched when viewCount > 0; shows when any episode has been viewed.
        """
        if not friend.access_token or not rating_keys:
            return {}
        watched: dict[int, str] = {}
        keys = sorted({int(k) for k in rating_keys})
        for start in range(0, len(keys), 100):
            batch = keys[start:start + 100]
            try:
                resp = requests.get(
                    f"{self.url}/library/metadata/{','.join(map(str, batch))}",
                    headers={"X-Plex-Token": friend.access_token, "Accept": "application/json"},
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                items = resp.json().get("MediaContainer", {}).get("Metadata", []) or []
            except Exception as exc:
                logger.warning("Watch-state lookup failed for %s: %s", friend.username, exc)
                continue
            for item in items:
                seen = int(item.get("viewCount") or 0) > 0 or int(item.get("viewedLeafCount") or 0) > 0
                if seen:
                    ts = item.get("lastViewedAt")
                    watched[int(item["ratingKey"])] = (
                        datetime.fromtimestamp(int(ts)).isoformat() if ts else datetime.now().isoformat()
                    )
        return watched

    # -- items / sections ---------------------------------------------------
    def section(self, section_id: int):
        if section_id not in self._sections:
            self._sections[section_id] = self.server.library.sectionByID(section_id)
        return self._sections[section_id]

    def fetch_items(self, rating_keys: list[int]) -> dict[int, object]:
        """Fetch items one by one; missing/deleted keys are simply absent.

        Deliberately not batched: a ``/library/metadata/a,b,c`` response carries
        the *container's* librarySectionID, so items from different libraries
        would all look like they belong to one section.
        """
        found: dict[int, object] = {}
        for key in sorted({int(k) for k in rating_keys}):
            try:
                found[key] = self.server.fetchItem(key)
            except NotFound:
                continue
            except Exception as exc:
                logger.debug("Could not fetch item %s: %s", key, exc)
        return found

    # -- collections --------------------------------------------------------
    def find_collection(self, section_id: int, label: str):
        section = self.section(section_id)
        for coll in section.collections():
            if any(l.tag.lower() == label.lower() for l in coll.labels) and coll.subtype == section.type:
                return coll
        return None

    def find_all_collections(self) -> list[tuple[int, object]]:
        """Every collection on the server carrying one of our labels."""
        out = []
        for sec in self.server.library.sections():
            if sec.type not in ("movie", "show"):
                continue
            for coll in sec.collections():
                if any(l.tag.lower().startswith(self.prefix.lower()) for l in coll.labels):
                    out.append((int(sec.key), coll))
        return out

    def duplicate_titled_collections(self) -> list[tuple[int, object]]:
        """Our collections whose title is shared with another of ours in the same section.

        Plex keys collection membership by title within a library, so two of
        our collections with one title are really one tag: they must all go.
        """
        by_title: dict[tuple[int, str], list] = {}
        for section_id, coll in self.find_all_collections():
            by_title.setdefault((section_id, coll.title.strip().lower()), []).append((section_id, coll))
        out = []
        for group in by_title.values():
            if len(group) > 1:
                out.extend(group)
        return out

    def get_collection(self, collection_key: int):
        try:
            coll = self.server.fetchItem(int(collection_key))
        except NotFound:
            return None
        return coll if coll.type == "collection" else None

    def create_collection(self, section_id: int, title: str, label: str, items: list):
        """Create + label + promote. Callers must have run ensure_exclusions() first."""
        section = self.section(section_id)
        coll = section.createCollection(title, items=items)
        coll.addLabel(label)
        coll.reload()
        self.promote(coll)
        return coll

    def promote(self, coll) -> None:
        hub = coll.visibility()
        if not hub.promotedToSharedHome:
            hub.promoteShared()
            hub = coll.visibility()
        try:
            hub.move()  # to the top of this library's Home rows
        except Exception as exc:
            logger.debug("Could not move hub %s to top: %s", coll.title, exc)

    def demote(self, coll) -> None:
        try:
            hub = coll.visibility()
            if hub.promotedToSharedHome:
                hub.demoteShared()
        except Exception as exc:
            logger.debug("Could not demote hub %s: %s", coll.title, exc)

    def set_members(self, coll, wanted: dict[int, object]) -> tuple[int, int]:
        """Reconcile collection membership to ``wanted`` {ratingKey: item}. Returns (added, removed)."""
        current = {int(i.ratingKey): i for i in coll.items()}
        to_add = [item for key, item in wanted.items() if key not in current]
        to_remove = [item for key, item in current.items() if key not in wanted]
        if to_add:
            coll.addItems(to_add)
        if to_remove:
            coll.removeItems(to_remove)
        return len(to_add), len(to_remove)

    def delete_collection(self, coll) -> None:
        self.demote(coll)
        coll.delete()

    def rename_collection(self, coll, title: str) -> None:
        if coll.title != title:
            coll.editTitle(title)
