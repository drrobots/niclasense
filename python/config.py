"""Load main.py's switches from an INI file.

Every command-line option is also a config key, and the mapping is derived from the
parser that main.py already builds rather than restated here -- add a flag there and it
is settable from a file with no change to this module. That is the whole reason `load()`
takes a parser: two lists of option names maintained side by side is precisely the drift
this file exists to avoid, the same argument `columns.py` makes about the schema.

Section headers are for the reader. `[capture]`, `[burst]` and `[plot]` group keys the way
the README groups the flags, but nothing enforces which key lives where, and keys before
any header are fine too -- a name under the "wrong" heading is still applied. Silently
ignoring a key that is spelled correctly and sitting in plain sight is the worst way for a
config file to fail. Unknown names, by contrast, are an error with a spelling suggestion,
because a typo would otherwise look exactly like a setting that had no effect.

Precedence is defaults < file < command line: the loaded values are handed to
`parser.set_defaults()`, so anything typed on the command line still wins. Two edges are
worth knowing, both inherited from argparse and both documented in the README:

- `--burst-on` is a repeatable flag, so command-line triggers *add* to the file's list
  rather than replacing it. Leave `burst_on` out of the file to give them a clean slate.
- The three on/off flags (`no_plot`, `no_autobaud`, `list_ports` is excluded) can be
  turned on by the file but not back off from the command line, since there is no
  `--plot` to counter `--no-plot` with.
"""

import argparse
import configparser
import difflib

# Keys before the first section header land here. Named something no one would type, so a
# file that does declare its own sections cannot collide with it.
_TOPLEVEL = "__toplevel__"

# Flags with no meaning in a file: the path to the file itself, and the two that print
# something and exit.
NOT_CONFIGURABLE = frozenset(("help", "config", "list_ports"))

_BOOLEANS = configparser.ConfigParser.BOOLEAN_STATES


class ConfigError(Exception):
    """A config file that could not be read, parsed, or understood."""


def load(path, parser):
    """Return {dest: value} for everything set in `path`, typed as `parser` expects.

    Raises ConfigError with a message meant to be shown as-is; main.py routes it through
    parser.error() so a bad config file fails like a bad flag does.
    """

    # parser._actions is the only way to ask argparse what it accepts. Private, but stable
    # across every 3.x, and the alternative is duplicating the option list.
    actions = {
        action.dest: action
        for action in parser._actions
        if action.dest not in NOT_CONFIGURABLE and action.dest != argparse.SUPPRESS
    }

    try:
        with open(path) as handle:
            text = handle.read()
    except (IOError, OSError) as exc:
        raise ConfigError("cannot read config %s: %s" % (path, exc.strerror or exc))

    ini = _parse(text, path)

    values = {}
    for section in ini.sections():
        for key, raw in ini.items(section):
            dest = key.replace("-", "_")
            action = actions.get(dest)
            if action is None:
                raise ConfigError(_unknown_key(path, key, actions))
            values[dest] = _convert(action, dest, raw, path)
    return values


def _parse(text, path):
    """configparser over `text`, with errors reported against the user's line numbers.

    A file that opens with keys rather than a `[section]` gets a synthetic header, which
    shifts every line configparser counts. Errors are rebuilt from the exception's own
    fields rather than its message so the shift comes back out -- a config error that
    points at the wrong line is worse than no line at all.
    """
    shift = 0
    if not _opens_with_section(text):
        text = "[%s]\n%s" % (_TOPLEVEL, text)
        shift = 1

    # interpolation=None because values are paths, endpoints and numbers, and a '%' in a
    # filename is not a substitution request.
    ini = configparser.ConfigParser(interpolation=None)
    try:
        ini.read_string(text, source=path)
    except configparser.ParsingError as exc:
        lineno, line = exc.errors[0]
        raise ConfigError(
            "%s: line %d is not `name = value`: %s" % (path, lineno - shift, line.strip())
        )
    except configparser.DuplicateOptionError as exc:
        raise ConfigError(
            "%s: line %d: %s is set twice" % (path, exc.lineno - shift, exc.option)
        )
    except configparser.DuplicateSectionError as exc:
        where = "" if exc.lineno is None else " line %d:" % (exc.lineno - shift)
        raise ConfigError("%s:%s section [%s] appears twice" % (path, where, exc.section))
    except configparser.Error as exc:
        raise ConfigError("%s: %s" % (path, _oneline(exc)))
    return ini


def _opens_with_section(text):
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped[0] in "#;":
            continue
        return stripped.startswith("[")
    return True  # nothing but blanks and comments; no header needed


def _convert(action, dest, raw, path):
    """Turn one INI string into the type the flag's own argparse action produces."""
    text = raw.strip()

    if action.nargs == 0:  # store_true / store_false
        return _boolean(dest, text, path)

    if isinstance(action, argparse._AppendAction):
        return _list(text)

    # --listen: a bare true means "yes, at the default endpoint", false means off, and
    # anything else is the endpoint itself. Mirrors the flag, which takes an optional value.
    if action.nargs == "?":
        if text.lower() in _BOOLEANS:
            return action.const if _BOOLEANS[text.lower()] else action.default
        return text

    if action.type is not None:
        try:
            return action.type(text)
        except (TypeError, ValueError):
            raise ConfigError(
                "%s: %s = %s is not a %s"
                % (path, dest, raw, getattr(action.type, "__name__", action.type))
            )
    return text


def _boolean(dest, text, path):
    try:
        return _BOOLEANS[text.lower()]
    except KeyError:
        raise ConfigError(
            "%s: %s = %s is not a yes/no value (true, false, yes, no, on, off, 1, 0)"
            % (path, dest, text)
        )


def _list(text):
    """Split a repeatable flag's value, one entry per line or comma-separated.

    An empty value yields an empty list, which every consumer here already reads as "use
    the defaults" -- so commenting out the entries of a list is the same as not naming it.
    """
    items = [
        piece.strip()
        for line in text.splitlines()
        for piece in line.split(",")
    ]
    return [item for item in items if item]


def _unknown_key(path, key, actions):
    near = difflib.get_close_matches(key.replace("-", "_"), sorted(actions), n=1)
    hint = " (did you mean %s?)" % near[0] if near else ""
    return "%s: unknown setting %r%s" % (path, key, hint)


def _oneline(exc):
    return " ".join(str(exc).split())
