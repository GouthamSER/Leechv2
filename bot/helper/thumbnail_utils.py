# This file is a part of Leechv2 (github.com/GouthamSER/Leechv2), based on NEO-WZML (github.com/irisXDR/NEO-WZML)

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

from bot import LOGGER
from bot.core.config_manager import Config
from bot.helper.ext_utils.bot_utils import sync_to_async


class ThumbnailFetcher:

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
                    # OMDb serves amazon-hosted posters pre-resized small
                    # (e.g. "..._V1_SX300.jpg"), which looks noticeably
                    # blurrier than the original. Strip the resize suffix
                    # so amazon serves the full-resolution image.
                    poster = re.sub(r'\._V1_.*?(\.\w+)$', r'\1', poster)
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

        poster_url = await cls.search_omdb(query, parsed.get('year'), is_tv=is_tv)

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
