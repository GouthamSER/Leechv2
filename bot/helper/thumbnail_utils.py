# This file is a part of NEO-WZML (github.com/irisXDR/NEO-WZML)

import re
import os
import warnings
import difflib
from os import path as ospath

# PTN v2.8.2 has invalid regex escape sequences in its internal files (extras.py,
# patterns.py, post.py) that trigger SyntaxWarnings on Python 3.13+. These are
# harmless — the patterns still match correctly. Suppress to keep logs clean.
with warnings.catch_warnings():
    warnings.filterwarnings('ignore', category=SyntaxWarning, module='PTN')
    import PTN
from aiohttp import ClientSession
from lxml.etree import HTML

from bot import LOGGER
from bot.core.config_manager import Config
from bot.helper.ext_utils.bot_utils import sync_to_async


class ThumbnailFetcher:

    TMDB_BASE_URL = "https://www.themoviedb.org"
    TMDB_API_BASE = "https://api.themoviedb.org/3"
    TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/original"
    VIDEO_EXTENSIONS = {
        '.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv',
        '.webm', '.m4v', '.mpg', '.mpeg', '.ts', '.mts', '.m2ts'
    }

    @staticmethod
    def is_video_file(filename: str) -> bool:
        return ospath.splitext(filename)[1].lower() in ThumbnailFetcher.VIDEO_EXTENSIONS

    @staticmethod
    def parse_filename(filename: str) -> dict:
        base_name = ospath.splitext(os.path.basename(filename))[0]
        ptn_result = PTN.parse(base_name)

        title = ptn_result.get('title', '').strip()
        year = ptn_result.get('year')  # returned as int by PTN
        season = ptn_result.get('season')
        episode = ptn_result.get('episode')
        episode_name = ptn_result.get('episodeName')

        is_tv = season is not None or episode is not None or bool(episode_name)

        # PTN sometimes leaves release-quality junk tags stuck inside 'title'
        # (e.g. "Achyuta Avataaram 2026 HQ"). Strip common ones out first, so
        # the year (which may come before the junk tag) can then be isolated.
        if title:
            junk_tags = (
                r'\b(HQ|HDR|HDRip|ORG|Original|Untouched|PreDVD|PRE|DVDScr|'
                r'HDCAM|CAM|TS|TC|Line|DUAL|MULTI|ESub|ESubs|Sub|Subbed)\b'
            )
            cleaned = re.sub(junk_tags, '', title, flags=re.IGNORECASE)
            cleaned = re.sub(r'\s+', ' ', cleaned).strip()
            if cleaned:
                title = cleaned

        # PTN sometimes fails to split a trailing year into the 'year' field and
        # leaves it stuck inside 'title' (e.g. "Idhayam Murali 2026"). Strip it out.
        if not year and title:
            trailing_year = re.search(r'\b(19\d{2}|20\d{2})\b\s*$', title)
            if trailing_year:
                year = int(trailing_year.group(1))
                title = title[:trailing_year.start()].strip()

        # Fallback: if PTN couldn't extract a meaningful title, do basic cleaning
        if not title or len(title) < 2:
            name = base_name
            year_match = re.search(r'\b(19|20)\d{2}\b', name)
            yr = str(year_match.group()) if year_match else None
            if yr:
                name = name.replace(yr, ' ').strip()
            name = re.sub(r'[._]', ' ', name)
            name = re.sub(r'\s+', ' ', name).strip()
            return {'name': name, 'year': yr, 'is_tv': False, 'season': None}

        return {
            'name': title,
            'year': str(year) if year else None,
            'is_tv': is_tv,
            'season': season,
        }

    @staticmethod
    def _normalize_title(title: str) -> str:
        if not title:
            return ''
        t = title.lower()
        t = re.sub(r'[^a-z0-9]+', ' ', t)
        return re.sub(r'\s+', ' ', t).strip()

    @classmethod
    def _titles_match(cls, query_name: str, candidate_title: str, threshold: float = 0.72) -> bool:
        """Guards against picking a wrong movie/show that merely ranked first
        in a fuzzy search. Requires the candidate title to actually resemble
        the parsed filename title, not just share the search slot."""
        a = cls._normalize_title(query_name)
        b = cls._normalize_title(candidate_title)
        if not a or not b:
            return False
        if a == b:
            return True
        ratio = difflib.SequenceMatcher(None, a, b).ratio()
        # also allow a clean containment match (e.g. "forest guard" in
        # "forest guard 2" would NOT match since a != b and ratio check below
        # still applies, but "the forest guard" containing "forest guard" is fine)
        if a in b or b in a:
            ratio = max(ratio, 0.8)
        return ratio >= threshold

    @staticmethod
    async def search_tmdb_api(query: str, year: str = None, is_tv: bool = False, season: int = None) -> str or None:
        """Fetch poster via official TMDB API (requires Config.TMDB_API_KEY)."""
        api_key = Config.TMDB_API_KEY
        if not api_key:
            return None
        try:
            search_types = ['tv', 'movie'] if is_tv else ['movie', 'tv']
            async with ClientSession() as session:
                for search_type in search_types:
                    params = {'api_key': api_key, 'query': query}
                    if year:
                        params['year' if search_type == 'movie' else 'first_air_date_year'] = year

                    url = f"{ThumbnailFetcher.TMDB_API_BASE}/search/{search_type}"
                    async with session.get(url, params=params, timeout=10) as resp:
                        if resp.status != 200:
                            LOGGER.debug(f"TMDB API search returned {resp.status}")
                            continue
                        data = await resp.json()

                    results = data.get('results') or []
                    if not results:
                        continue

                    # Old code always grabbed results[0]. TMDB's own ranking
                    # can put a same-named remake/short/unrelated title
                    # first, especially once the year filter above finds
                    # nothing and we fall through to an unfiltered search.
                    # Pick the first candidate whose title actually matches,
                    # preferring one whose release year lines up.
                    item = None
                    yr_int = int(year) if year else None
                    best_year_match = None
                    for cand in results[:5]:
                        cand_title = cand.get('title') or cand.get('name') or ''
                        if not ThumbnailFetcher._titles_match(query, cand_title):
                            continue
                        cand_date = cand.get('release_date') or cand.get('first_air_date') or ''
                        cand_year = int(cand_date[:4]) if cand_date[:4].isdigit() else None
                        if yr_int and cand_year == yr_int:
                            item = cand
                            break
                        if best_year_match is None:
                            best_year_match = cand
                    if item is None:
                        item = best_year_match
                    if item is None:
                        LOGGER.debug(f"TMDB API: no title-matching result for '{query}' via {search_type}")
                        continue
                    tmdb_id = item.get('id')

                    if search_type == 'tv' and season and tmdb_id:
                        season_url = f"{ThumbnailFetcher.TMDB_API_BASE}/tv/{tmdb_id}/season/{season}"
                        async with session.get(season_url, params={'api_key': api_key}, timeout=10) as s_resp:
                            if s_resp.status == 200:
                                s_data = await s_resp.json()
                                poster = s_data.get('poster_path')
                                if poster:
                                    return f"{ThumbnailFetcher.TMDB_IMAGE_BASE}{poster}"

                    backdrop = item.get('backdrop_path')
                    poster = item.get('poster_path')
                    chosen = backdrop or poster
                    if chosen:
                        LOGGER.info(f"TMDB API poster found for '{query}' via {search_type}")
                        return f"{ThumbnailFetcher.TMDB_IMAGE_BASE}{chosen}"

            return None
        except Exception as e:
            LOGGER.error(f"TMDB API search error: {e}")
            return None

    @staticmethod
    async def search_tmdb(query: str, year: str = None, is_tv: bool = False, season: int = None) -> str or None:
        try:
            from urllib.parse import quote

            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept-Language': 'en-US,en;q=0.9'
            }

            search_types = ['tv', 'movie'] if is_tv else ['movie', 'tv']

            async with ClientSession() as session:
                for search_type in search_types:
                    for try_year in ([year, None] if year else [None]):
                        if try_year is None and year is not None:
                            search_query = re.sub(
                                r'\b' + re.escape(str(year)) + r'\b', '',
                                query
                            ).strip()
                        else:
                            search_query = query

                        search_url = f"{ThumbnailFetcher.TMDB_BASE_URL}/search/{search_type}?query={quote(search_query)}"

                        if try_year and search_type == 'movie':
                            search_url += f"&year={try_year}"
                        elif try_year and search_type == 'tv':
                            search_url += f"&first_air_date_year={try_year}"

                        LOGGER.debug(f"TMDB search URL: {search_url}")

                        async with session.get(search_url, headers=headers, ssl=False, timeout=10) as resp:
                            if resp.status != 200:
                                continue
                            html_content = await resp.text()

                        html = HTML(html_content)

                        if search_type == 'tv' and season:
                            show_links = [
                                l for l in html.xpath('//a[contains(@href, "/tv/")]/@href')
                                if re.search(r'/tv/\d+', l)
                            ]
                            if show_links:
                                show_path = show_links[0]

                                backdrop_url = await ThumbnailFetcher._try_fetch_backdrop(
                                    session, show_path, headers
                                )
                                if backdrop_url:
                                    return backdrop_url

                                season_url = f"{ThumbnailFetcher.TMDB_BASE_URL}{show_path}/season/{season}"
                                LOGGER.info(f"TMDB fetching season {season} poster from: {season_url}")

                                async with session.get(season_url, headers=headers, ssl=False, timeout=10) as season_resp:
                                    if season_resp.status == 200:
                                        season_html_content = await season_resp.text()
                                        season_html = HTML(season_html_content)

                                        season_posters = season_html.xpath('//div[contains(@class, "poster")]//img/@src')
                                        if not season_posters:
                                            season_posters = season_html.xpath('//img[contains(@src, "/t/p/")]/@src')

                                        if season_posters:
                                            poster_path = season_posters[0]
                                            poster_match = re.search(r'/t/p/[^/]+/(.+)', poster_path)
                                            if poster_match:
                                                poster_filename = poster_match.group(1)
                                                full_url = f"{ThumbnailFetcher.TMDB_IMAGE_BASE}/{poster_filename}"
                                                LOGGER.info(f"TMDB season {season} poster URL: {full_url}")
                                                return full_url

                        poster_nodes = html.xpath('//div[contains(@class, "poster")]//img')
                        if not poster_nodes:
                            poster_nodes = html.xpath('//a[@data-id]/img')
                        if not poster_nodes:
                            poster_nodes = html.xpath('//img[contains(@src, "/t/p/")]')

                        # The scraper used to grab posters[0] unconditionally.
                        # TMDB's search page ranking isn't always the right
                        # movie, especially on the "retry without year" pass,
                        # so verify against the poster's own alt/title text
                        # before accepting it. When TMDB's server-side year
                        # filter (try_year) was applied we trust it a bit
                        # more since that already narrowed candidates.
                        posters = []
                        for node in poster_nodes:
                            src = node.get('src')
                            if not src:
                                continue
                            alt_text = (node.get('alt') or node.get('title') or '').strip()
                            if alt_text:
                                if ThumbnailFetcher._titles_match(search_query, alt_text):
                                    posters = [src]
                                    break
                                continue
                            if try_year:
                                posters = [src]
                                break
                        if not posters:
                            LOGGER.debug(
                                f"TMDB scraper: no title-verified poster for '{search_query}' "
                                f"(type={search_type}, year={try_year})"
                            )

                        if posters:
                            detail_links = [
                                l for l in html.xpath(
                                    f'//a[contains(@href, "/{search_type}/")]/@href'
                                )
                                if re.search(rf'/{search_type}/\d+', l)
                            ]
                            if detail_links:
                                backdrop_url = await ThumbnailFetcher._try_fetch_backdrop(
                                    session, detail_links[0], headers
                                )
                                if backdrop_url:
                                    return backdrop_url

                            poster_path = posters[0]
                            LOGGER.debug(f"TMDB found poster path: {poster_path}")

                            poster_match = re.search(r'/t/p/[^/]+/(.+)', poster_path)
                            if poster_match:
                                poster_filename = poster_match.group(1)
                                full_url = f"{ThumbnailFetcher.TMDB_IMAGE_BASE}/{poster_filename}"
                                LOGGER.info(f"TMDB poster URL (original quality): {full_url}")
                                return full_url

                            if poster_path.startswith('http'):
                                upgraded = re.sub(r'/t/p/[^/]+/', '/t/p/original/', poster_path)
                                LOGGER.info(f"TMDB poster URL (upgraded): {upgraded}")
                                return upgraded

                        if try_year is not None:
                            LOGGER.info(f"TMDB: No poster with year={try_year}, retrying without year filter (query: '{search_query}')")

            return None

        except Exception as e:
            LOGGER.error(f"TMDB search error: {e}")
            return None

    @staticmethod
    async def _try_fetch_backdrop(session: ClientSession, detail_path: str, headers: dict) -> str or None:
        """Fetch a landscape backdrop from a TMDB movie/TV detail page."""
        try:
            backdrop_url = f"{ThumbnailFetcher.TMDB_BASE_URL}{detail_path}/images/backdrops"
            LOGGER.info(f"TMDB fetching backdrop from: {backdrop_url}")

            async with session.get(backdrop_url, headers=headers, ssl=False, timeout=10) as resp:
                if resp.status != 200:
                    LOGGER.debug(f"TMDB backdrop page returned {resp.status}")
                    return None
                html_content = await resp.text()

            html = HTML(html_content)

            backdrop_links = html.xpath(
                '//img[contains(@src, "w500_and_h282_face")]/ancestor::a[1]/@href'
            )
            if not backdrop_links:
                LOGGER.debug("TMDB no backdrop images found on gallery page")
                return None

            backdrop_path = backdrop_links[0]
            LOGGER.debug(f"TMDB found backdrop link: {backdrop_path}")
            LOGGER.info(f"TMDB backdrop URL: {backdrop_path}")
            return backdrop_path

        except Exception as e:
            LOGGER.error(f"TMDB backdrop fetch error: {e}")
            return None

    @staticmethod
    async def download_poster(url: str, user_id: int) -> str or None:
        try:
            import tempfile
            from PIL import Image

            fd, temp_path = tempfile.mkstemp(suffix='.jpg', prefix=f'aut_thumb_{user_id}_')
            try:
                os.close(fd)
            except Exception:
                pass

            async with ClientSession() as session:
                async with session.get(url, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                }, timeout=15) as resp:
                    if resp.status != 200:
                        return None
                    content = await resp.read()

            def save_image():
                from io import BytesIO
                img = Image.open(BytesIO(content))
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                img.save(temp_path, 'JPEG', quality=95)
                return temp_path

            return await sync_to_async(save_image)

        except Exception as e:
            LOGGER.error(f"Poster download error: {e}")
            return None

    @staticmethod
    async def search_omdb(query: str, year: str = None, is_tv: bool = False) -> str or None:
        """Fallback poster search via OMDb API (requires Config.OMDB_API_KEY)."""
        api_key = Config.OMDB_API_KEY
        if not api_key:
            return None
        try:
            omdb_type = 'series' if is_tv else 'movie'
            async with ClientSession() as session:

                async def _lookup(with_year: bool):
                    params = {'apikey': api_key, 't': query, 'type': omdb_type}
                    if with_year and year:
                        params['y'] = year
                    async with session.get(
                        "https://www.omdbapi.com/", params=params, timeout=10
                    ) as resp:
                        if resp.status != 200:
                            LOGGER.debug(f"OMDb API returned {resp.status}")
                            return None
                        return await resp.json()

                data = await _lookup(with_year=True)
                if not data or data.get('Response') != 'True':
                    # OMDb's exact-title-match endpoint is strict about the
                    # year; if a year gave nothing, retry without it, but
                    # still verify the title below before trusting it.
                    data = await _lookup(with_year=False)

                if not data or data.get('Response') != 'True':
                    LOGGER.debug(f"OMDb: no result for '{query}'")
                    return None

                cand_title = data.get('Title') or ''
                if not ThumbnailFetcher._titles_match(query, cand_title):
                    LOGGER.debug(f"OMDb: title mismatch for '{query}' -> got '{cand_title}'")
                    return None

                if year:
                    cand_year_raw = (data.get('Year') or '').split('\u2013')[0].split('-')[0].strip()
                    if cand_year_raw.isdigit() and abs(int(cand_year_raw) - int(year)) > 1:
                        LOGGER.debug(
                            f"OMDb: title matched but year {year} not close to {cand_year_raw} for '{query}'"
                        )
                        return None

                poster = data.get('Poster')
                if poster and poster != 'N/A':
                    LOGGER.info(f"OMDb poster found for '{query}'")
                    return poster
                return None

        except Exception as e:
            LOGGER.error(f"OMDb search error: {e}")
            return None

    @classmethod
    async def fetch_thumbnail(cls, filename: str, user_id: int) -> str or None:
        if not cls.is_video_file(filename):
            LOGGER.debug(f"Auto-thumbnail: Skipping non-video file: {filename}")
            return None

        parsed = cls.parse_filename(filename)
        if not parsed['name'] or len(parsed['name']) < 3:
            LOGGER.debug(f"Auto-thumbnail: Could not extract valid name from: {filename}")
            return None

        is_tv = parsed.get('is_tv', False)
        season = parsed.get('season')
        # NOTE: query is the bare title only. Year must stay OUT of the search
        # text and travel as its own param - the old code appended it here
        # ("Forest Guard 2026") which made every provider search for that
        # literal string, tanked match quality, and led to random top-ranked
        # results (wrong movie's poster) being accepted with no title check.
        query = parsed['name']

        LOGGER.info(f"Auto-thumbnail: Searching for '{query}' (TV: {is_tv}, Season: {season}, Year: {parsed.get('year')})")

        poster_url = await cls.search_tmdb_api(query, parsed.get('year'), is_tv=is_tv, season=season)
        if not poster_url:
            poster_url = await cls.search_omdb(query, parsed.get('year'), is_tv=is_tv)
        if not poster_url:
            poster_url = await cls.search_tmdb(query, parsed.get('year'), is_tv=is_tv, season=season)

        if poster_url:
            thumbnail_path = await cls.download_poster(poster_url, user_id)
            if thumbnail_path:
                LOGGER.info(f"Auto-thumbnail: Successfully fetched poster for '{query}'")
                return thumbnail_path

        LOGGER.warning(f"Auto-thumbnail: No poster found for '{query}'")
        return None

    @staticmethod
    async def cleanup_thumbnail(thumb_path: str):
        try:
            if thumb_path and ospath.exists(thumb_path):
                from aiofiles.os import remove
                await remove(thumb_path)
                LOGGER.debug(f"Auto-thumbnail: Cleaned up {thumb_path}")
        except Exception as e:
            LOGGER.error(f"Auto-thumbnail cleanup error: {e}")
