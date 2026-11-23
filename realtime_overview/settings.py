from krita import Krita

GROUP = "realtime_overview"


def read(key, default=""):
    return Krita.instance().readSetting(GROUP, key, str(default))


def write(key, value):
    Krita.instance().writeSetting(GROUP, key, str(value))


def read_bool(key, default):
    return read(key, "true" if default else "false").lower() == "true"


def write_bool(key, value):
    write(key, "true" if value else "false")


def read_int(key, default):
    try:
        return int(read(key, default))
    except ValueError:
        return default
