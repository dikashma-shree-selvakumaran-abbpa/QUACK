"""Build provenance stamped in by quack.spec at freeze time.

The values checked into git mark a source (non-frozen) run; PyInstaller
overwrites this module with the real commit and build date.
"""

BUILD_COMMIT = "source"
BUILD_DATE = ""
