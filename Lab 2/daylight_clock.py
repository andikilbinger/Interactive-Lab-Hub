# daylight_clock.py -- a clock that tells the time as a sky rather than a readout.
#
# The whole 24 hours are one ring: noon at the top, midnight at the bottom. The
# lit part of the ring is the part of today the sun is actually up, so the arc
# visibly shortens as the term goes on. A sun rides the ring and becomes a moon
# when it sets, and the background is the colour of the sky right now.
#
# The colour comes from the sun's real elevation, computed for this date and
# latitude, not from the clock hour -- otherwise the sky would still be blue an
# hour after a December sunset. The same elevation is warmer going up than
# coming down, so mornings run yellow and evenings run red.
#
# Button A (GPIO23) cycles the face, button B (GPIO24) toggles 12/24 hour.

import math
import time
from collections import namedtuple

import board
import digitalio
from PIL import Image, ImageDraw, ImageFont
import adafruit_rgb_display.st7789 as st7789

# Where the clock thinks it is. Cornell Tech, Roosevelt Island.
LAT = 40.7561
LON = -73.9500

# --- Display (same wiring as the other Lab 2 scripts: CS on GPIO5) ----------
cs_pin = digitalio.DigitalInOut(board.D5)
dc_pin = digitalio.DigitalInOut(board.D25)
reset_pin = None
BAUDRATE = 64000000

spi = board.SPI()
disp = st7789.ST7789(
    spi,
    cs=cs_pin,
    dc=dc_pin,
    rst=reset_pin,
    baudrate=BAUDRATE,
    width=135,
    height=240,
    x_offset=53,
    y_offset=40,
)

# Swap height/width to rotate the canvas into landscape, as stats.py does.
HEIGHT = disp.width
WIDTH = disp.height
ROTATION = 90

image = Image.new("RGB", (WIDTH, HEIGHT))
draw = ImageDraw.Draw(image)

FONT_DIR = "/usr/share/fonts/truetype/dejavu/"
font_big = ImageFont.truetype(FONT_DIR + "DejaVuSans-Bold.ttf", 34)
# Smaller inside the ring, so the digits never crowd the outline.
font_ring = ImageFont.truetype(FONT_DIR + "DejaVuSans-Bold.ttf", 28)
font_mid = ImageFont.truetype(FONT_DIR + "DejaVuSans.ttf", 17)
font_small = ImageFont.truetype(FONT_DIR + "DejaVuSans.ttf", 12)

backlight = digitalio.DigitalInOut(board.D22)
backlight.switch_to_output(value=True)

# Buttons read LOW when pressed, because of the internal pull-ups.
button_a = digitalio.DigitalInOut(board.D23)
button_b = digitalio.DigitalInOut(board.D24)
button_a.switch_to_input(pull=digitalio.Pull.UP)
button_b.switch_to_input(pull=digitalio.Pull.UP)


# --- Where the sun actually is ---------------------------------------------
# NOAA's solar position algorithm, which is plenty accurate for colouring a
# 240x135 screen. Everything is in degrees unless it says otherwise.

Sun = namedtuple("Sun", "elevation rising sunrise sunset")


def _julian_day(now, gmt_offset_hours):
    """Julian day from a local struct_time and its UTC offset."""
    year, month = now.tm_year, now.tm_mon
    day = now.tm_mday + (
        now.tm_hour + now.tm_min / 60.0 + now.tm_sec / 3600.0 - gmt_offset_hours
    ) / 24.0
    if month <= 2:
        year -= 1
        month += 12
    a = year // 100
    b = 2 - a + a // 4
    return (
        int(365.25 * (year + 4716))
        + int(30.6001 * (month + 1))
        + day
        + b
        - 1524.5
    )


def solar(now):
    """The sun's elevation now, whether it is climbing, and today's sunrise/sunset.

    Sunrise and sunset come back as fractions of the day, or None on a day when
    the sun never crosses the horizon (which this latitude never sees, but the
    arithmetic would otherwise raise).
    """
    gmt_offset = now.tm_gmtoff / 3600.0
    jc = (_julian_day(now, gmt_offset) - 2451545.0) / 36525.0

    # Sun's geometric mean longitude and anomaly.
    mean_long = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360
    mean_anom = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    eccentricity = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)

    center = (
        math.sin(math.radians(mean_anom))
        * (1.914602 - jc * (0.004817 + 0.000014 * jc))
        + math.sin(math.radians(2 * mean_anom)) * (0.019993 - 0.000101 * jc)
        + math.sin(math.radians(3 * mean_anom)) * 0.000289
    )

    omega = 125.04 - 1934.136 * jc
    apparent_long = (
        mean_long + center - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    )

    obliquity = (
        23
        + (26 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60) / 60
        + 0.00256 * math.cos(math.radians(omega))
    )

    declination = math.degrees(
        math.asin(
            math.sin(math.radians(obliquity)) * math.sin(math.radians(apparent_long))
        )
    )

    # Equation of time, in minutes -- the sun runs early or late against the clock.
    var_y = math.tan(math.radians(obliquity / 2)) ** 2
    eot = 4 * math.degrees(
        var_y * math.sin(2 * math.radians(mean_long))
        - 2 * eccentricity * math.sin(math.radians(mean_anom))
        + 4
        * eccentricity
        * var_y
        * math.sin(math.radians(mean_anom))
        * math.cos(2 * math.radians(mean_long))
        - 0.5 * var_y * var_y * math.sin(4 * math.radians(mean_long))
        - 1.25 * eccentricity * eccentricity * math.sin(2 * math.radians(mean_anom))
    )

    minutes = now.tm_hour * 60 + now.tm_min + now.tm_sec / 60.0
    true_solar = (minutes + eot + 4 * LON - 60 * gmt_offset) % 1440
    hour_angle = true_solar / 4 - 180

    zenith = math.degrees(
        math.acos(
            max(
                -1.0,
                min(
                    1.0,
                    math.sin(math.radians(LAT)) * math.sin(math.radians(declination))
                    + math.cos(math.radians(LAT))
                    * math.cos(math.radians(declination))
                    * math.cos(math.radians(hour_angle)),
                ),
            )
        )
    )
    elevation = 90 - zenith

    # 90.833 rather than 90 accounts for refraction and the sun's own width.
    sunrise = sunset = None
    try:
        ha_horizon = math.degrees(
            math.acos(
                math.cos(math.radians(90.833))
                / (math.cos(math.radians(LAT)) * math.cos(math.radians(declination)))
                - math.tan(math.radians(LAT)) * math.tan(math.radians(declination))
            )
        )
        noon = 720 - 4 * LON - eot + 60 * gmt_offset
        sunrise = ((noon - 4 * ha_horizon) / 1440.0) % 1.0
        sunset = ((noon + 4 * ha_horizon) / 1440.0) % 1.0
    except ValueError:
        pass  # midnight sun or polar night

    # Before local solar noon the sun is still climbing.
    return Sun(elevation, hour_angle < 0, sunrise, sunset)


# --- The sky ---------------------------------------------------------------
# Colour keyed to the sun's elevation in degrees, not to the clock. Two ramps,
# because the same height of sun reads warm going up and red coming down. Keep
# every leg of each walk on one side of grey: interpolating a warm colour
# straight to a cool one passes through mud.

MORNING = [
    (-18, (10, 12, 34)),      # astronomical night
    (-12, (30, 32, 74)),      # the sky starting to thin
    (-6, (92, 84, 140)),      # civil twilight, still violet
    (-2, (206, 140, 120)),    # first blush
    (0, (244, 178, 110)),     # sunrise
    (6, (250, 206, 130)),     # warm morning yellow
    (18, (248, 212, 150)),    # still a morning sky, not yet a daytime one
    (28, (216, 212, 196)),    # haze, the one bright neutral of the day
    (38, (166, 198, 232)),    # morning blue
    (50, (126, 186, 242)),    # full day
    (60, (96, 166, 240)),     # high sun
]

EVENING = [
    (60, (96, 166, 240)),     # high sun
    (32, (124, 184, 238)),    # afternoon
    (20, (170, 190, 222)),    # the light going long
    (12, (226, 186, 150)),    # golden haze
    (5, (246, 148, 86)),      # golden hour
    (0, (232, 104, 70)),      # sunset
    (-2, (196, 74, 82)),      # the red minute
    (-6, (120, 58, 102)),     # afterglow
    (-12, (48, 40, 88)),      # nautical twilight
    (-18, (10, 12, 34)),      # night
]

SUN_DISC = (255, 214, 120)
MOON_DISC = (232, 236, 246)

CENTER = (WIDTH // 2, HEIGHT // 2 + 2)
RADIUS = 54


def mix(color_a, color_b, t):
    """Blend two RGB tuples, t running 0 -> 1."""
    return tuple(int(round(a + (b - a) * t)) for a, b in zip(color_a, color_b))


def sky_color(sun):
    """The sky colour for the sun's current elevation."""
    ramp = MORNING if sun.rising else EVENING
    # MORNING climbs, EVENING falls; walk each in its own direction.
    for i in range(len(ramp) - 1):
        e0, c0 = ramp[i]
        e1, c1 = ramp[i + 1]
        low, high = min(e0, e1), max(e0, e1)
        if low <= sun.elevation <= high:
            return mix(c0, c1, (sun.elevation - e0) / (e1 - e0))
    # Past either end of the ramp.
    return ramp[0][1] if sun.elevation < ramp[0][0] else ramp[-1][1]


def ink(background):
    """Black or white text, whichever the background can carry."""
    r, g, b = background
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    return (20, 20, 28) if luminance > 150 else (245, 245, 250)


def centered(text, font, y, fill):
    """Draw text centered horizontally at a given y."""
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(((WIDTH - (box[2] - box[0])) / 2 - box[0], y), text, font=font, fill=fill)


def ring_angle(day_fraction):
    """Ring angle for a fraction of the day. Noon top, midnight bottom."""
    return (90 - day_fraction * 360) % 360


def point_on_ring(day_fraction, radius=RADIUS):
    angle = math.radians(ring_angle(day_fraction))
    return (
        CENTER[0] + radius * math.cos(angle),
        CENTER[1] + radius * math.sin(angle),
    )


def draw_sky_face(now, day_fraction, sun, background, foreground):
    """Horizon, ring, the lit arc of today, and the sun or moon riding it."""
    box = (
        CENTER[0] - RADIUS,
        CENTER[1] - RADIUS,
        CENTER[0] + RADIUS,
        CENTER[1] + RADIUS,
    )

    # The whole 24 hours, faint.
    draw.ellipse(box, outline=mix(background, foreground, 0.22))

    # The part of today the sun is actually above the horizon, brighter. In
    # September this is about thirteen hours; by December it is nine.
    if sun.sunrise is not None:
        # PIL sweeps an arc clockwise from start to end, and noon sits at the
        # top of this ring -- so the lit half runs sunset -> sunrise, not the
        # other way about. Reversed, this highlights the night instead.
        draw.arc(
            box,
            start=ring_angle(sun.sunset),
            end=ring_angle(sun.sunrise),
            fill=mix(background, SUN_DISC, 0.75),
            width=3,
        )
        # The horizon, drawn between the real sunrise and sunset rather than
        # across a fixed 06:00/18:00 axis, so it tilts through the year.
        draw.line(
            point_on_ring(sun.sunrise) + point_on_ring(sun.sunset),
            fill=mix(background, foreground, 0.28),
        )

    for hour in range(24):
        angle = math.radians(ring_angle(hour / 24.0))
        length = 5 if hour % 6 == 0 else 2
        draw.line(
            (
                CENTER[0] + (RADIUS - length) * math.cos(angle),
                CENTER[1] + (RADIUS - length) * math.sin(angle),
                CENTER[0] + RADIUS * math.cos(angle),
                CENTER[1] + RADIUS * math.sin(angle),
            ),
            fill=mix(background, foreground, 0.4),
        )

    # The light itself, sun while it is up and moon once it is down.
    x, y = point_on_ring(day_fraction)
    body = SUN_DISC if sun.elevation > 0 else MOON_DISC
    for glow, alpha in ((11, 0.25), (8, 0.55)):
        draw.ellipse(
            (x - glow, y - glow, x + glow, y + glow),
            fill=mix(background, body, alpha),
        )
    draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=body)


def face_sky(now, day_fraction, sun, background, foreground, use_24h):
    draw_sky_face(now, day_fraction, sun, background, foreground)
    clock = time.strftime("%H:%M" if use_24h else "%I:%M", now).lstrip("0")
    centered(clock, font_ring, CENTER[1] - 23, foreground)
    label = time.strftime("%a %d %b", now) if use_24h else time.strftime("%p", now)
    centered(label, font_small, CENTER[1] + 14, mix(background, foreground, 0.7))


def face_digits(now, day_fraction, sun, background, foreground, use_24h):
    clock = time.strftime("%H:%M:%S" if use_24h else "%I:%M:%S %p", now)
    centered(clock, font_big, 30, foreground)
    centered(
        time.strftime("%A, %d %B", now), font_mid, 76, mix(background, foreground, 0.75)
    )

    # Today's daylight, and how far through it we are.
    if sun.sunrise is not None:
        length = (sun.sunset - sun.sunrise) % 1.0
        hours, minutes = divmod(int(round(length * 24 * 60)), 60)
        detail = "{}h{:02d}m of light  {}  {}".format(
            hours,
            minutes,
            time.strftime("%H:%M", time.localtime(_seconds_today(now, sun.sunrise))),
            time.strftime("%H:%M", time.localtime(_seconds_today(now, sun.sunset))),
        )
        centered(detail, font_small, 100, mix(background, foreground, 0.65))

    margin = 24
    span = WIDTH - 2 * margin
    draw.rectangle((margin, 118, margin + span, 123), fill=mix(background, foreground, 0.2))
    draw.rectangle(
        (margin, 118, margin + span * day_fraction, 123),
        fill=SUN_DISC if sun.elevation > 0 else MOON_DISC,
    )


def _seconds_today(now, fraction):
    """Epoch seconds for a fraction of the day now falls in."""
    midnight = time.mktime(
        (now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, now.tm_isdst)
    )
    return midnight + fraction * 86400


def face_units(now, day_fraction, sun, background, foreground, use_24h):
    """The day measured in things other than hours."""
    seconds = day_fraction * 86400
    rows = [
        ("of today spent", "{:.2f}%".format(day_fraction * 100)),
        ("sun, degrees up", "{:+.1f}".format(sun.elevation)),
        ("coffees deep", "{:.1f}".format(max(0.0, (seconds - 25200) / 14400))),
    ]
    y = 18
    for label, value in rows:
        centered(value, font_mid, y, foreground)
        centered(label, font_small, y + 20, mix(background, foreground, 0.65))
        y += 39


FACES = (face_sky, face_digits, face_units)


def render(now, face, use_24h):
    """Draw one moment. `now` is a struct_time, real or simulated."""
    day_fraction = (now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec) / 86400.0
    sun = solar(now)
    background = sky_color(sun)
    foreground = ink(background)

    draw.rectangle((0, 0, WIDTH, HEIGHT), fill=background)
    FACES[face](now, day_fraction, sun, background, foreground, use_24h)
    disp.image(image, ROTATION)


def midnight_of(now):
    """Epoch seconds for the start of the day `now` falls in."""
    return time.mktime(
        (now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1)
    )


class Buttons:
    """Edge-detected buttons, so a hold counts once."""

    def __init__(self):
        self.last_a = True
        self.last_b = True
        self.last_both = False

    def read(self):
        a, b = button_a.value, button_b.value
        both = (not a) and (not b)
        # A fresh both-press swallows the individual edges under it.
        pressed_both = both and not self.last_both
        edge_a = self.last_a and not a and not both
        edge_b = self.last_b and not b and not both
        self.last_a, self.last_b, self.last_both = a, b, both
        return edge_a, edge_b, pressed_both


def sweep_day(seconds, midnight, face, use_24h, buttons):
    """Run one whole day past the screen in `seconds`. Returns (face, use_24h)."""
    started = time.monotonic()
    while True:
        progress = (time.monotonic() - started) / seconds
        if progress >= 1.0:
            return face, use_24h
        render(time.localtime(midnight + progress * 86400), face, use_24h)
        edge_a, edge_b, both = buttons.read()
        if edge_a:
            face = (face + 1) % len(FACES)
        if edge_b:
            use_24h = not use_24h
        if both:
            return face, use_24h  # let a second both-press cut the sweep short


def main(demo_seconds=None, demo_date=None):
    face = 0
    use_24h = True
    buttons = Buttons()

    if demo_seconds:
        # Loop the same day forever, so it can just be left running for filming.
        midnight = (
            midnight_of(demo_date) if demo_date else midnight_of(time.localtime())
        )
        while True:
            face, use_24h = sweep_day(demo_seconds, midnight, face, use_24h, buttons)
        return

    while True:
        render(time.localtime(), face, use_24h)

        edge_a, edge_b, both = buttons.read()
        if edge_a:
            face = (face + 1) % len(FACES)
        if edge_b:
            use_24h = not use_24h
        if both:
            # Hold both buttons for a one-off fast-forward through today.
            face, use_24h = sweep_day(
                20, midnight_of(time.localtime()), face, use_24h, buttons
            )

        time.sleep(0.08)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="A clock that shows the time as the sky."
    )
    parser.add_argument(
        "--demo",
        nargs="?",
        const=20.0,
        type=float,
        metavar="SECONDS",
        help="sweep a whole day in SECONDS (default 20) and loop",
    )
    parser.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        help="the day to simulate in demo mode, e.g. 2026-12-21 (default today)",
    )
    args = parser.parse_args()

    day = None
    if args.date:
        parsed = time.strptime(args.date, "%Y-%m-%d")
        day = time.localtime(
            time.mktime(
                (parsed.tm_year, parsed.tm_mon, parsed.tm_mday, 12, 0, 0, 0, 0, -1)
            )
        )

    try:
        main(demo_seconds=args.demo, demo_date=day)
    except KeyboardInterrupt:
        draw.rectangle((0, 0, WIDTH, HEIGHT), fill=(0, 0, 0))
        disp.image(image, ROTATION)
        backlight.value = False
