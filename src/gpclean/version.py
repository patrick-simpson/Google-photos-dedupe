"""Version constants shared by every stage.

EXTRACT_VERSION is bumped by hand whenever scan output changes meaning; it is part of the
scan config hash, so bumping it forces a rescan. Doc or merge-only changes must NOT bump it.
"""

CODE_VERSION = "0.1.0"
EXTRACT_VERSION = 1
SIG_VERSION = 1
INDEX_SCHEMA = 1
REVIEW_SCHEMA = 1

# Google Photos trash retention (changed from 60 to 30 days in Sept 2026;
# support.google.com/photos/answer/10100180). Used by the README text and the review site.
TRASH_DAYS = 30
