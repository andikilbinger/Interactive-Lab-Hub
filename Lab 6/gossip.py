#!/usr/bin/env python3
"""
gossip.py: Raspberry Pis that gossip with each other over MQTT.

How it works
  * Conversation (MQTT): every Pi publishes a "hello" every 2 s listing the
    rumors it knows. When a friend is missing a rumor, it gets whispered to
    them on their own topic. A Pi that joins late gets caught up, the same way
    gossip spreads in real life (and how "gossip protocols" work in
    distributed systems).
  * Starting rumors (APDS-9960 gesture, --apds): swipe over the sensor and the
    Pi comes up with a new rumor, which then spreads to the others.
  * Eavesdropper detector (VCNL4040, --vcnl): if a person leans in close, the
    Pi hisses "shh, someone's listening" and tells its friends. The whole group
    makes small talk until the person backs off.
  * Optional Bluetooth proximity (--ble): only Pis physically close to each
    other (by signal strength) count as "nearby" and gossip.

MQTT topics (broker farlab.infosci.cornell.edu, all under IDD/mamaya/gossip/)
  hello/<pi>    {"known": [rumor ids], "hush": bool}   every 2 s from every Pi
  rumor/<to>    {"from", "rid", "text", "origin"}     a rumor whispered to one Pi
  hush/<pi>     "1" / "0"                              eavesdropper seen / gone
  bye/<pi>      ""                                     sent by the broker if a Pi drops off

Run:   python3 gossip.py --apds                    # gossiper: swipe to start a rumor
       python3 gossip.py --vcnl --speak            # lookout: VCNL4040 + speaker
       python3 gossip.py --calibrate               # print VCNL4040 readings to pick a threshold
       python3 gossip.py --rumor "Pi #2 has a crush on the toaster"
       python3 gossip.py --ble                     # also use Bluetooth signal strength for "nearby"

While it's running, type a line and press Enter to start a new rumor.
"""

import argparse
import asyncio
import json
import random
import shutil
import socket
import subprocess
import sys
import time
import uuid

import paho.mqtt.client as mqtt

MQTT_BROKER = "farlab.infosci.cornell.edu"
MQTT_PORT = 1883
MQTT_USERNAME = "idd"
MQTT_PASSWORD = "device@theFarm"
BASE = "IDD/mamaya/gossip"

SERVICE_UUID = "6a1f0001-9c3e-4b7a-8f2d-60551905a1e0"
CHAR_UUID = "6a1f0002-9c3e-4b7a-8f2d-60551905a1e0"

HELLO_EVERY = 2.0  # seconds between "hello" messages
FORGET_AFTER = 8.0  # seconds without hearing from a peer => they've left
LISTENER_GONE_AFTER = 2.5  # seconds with nobody close before gossip resumes

STARTER_RUMORS = [
    "the router has been talking to someone else's laptop",
    "the kettle in the kitchen is secretly a smart kettle",
    "someone saw the printer jam on purpose",
    "the motion sensor by the door is faking it",
    "the old Pi in the drawer is coming back",
    "the smart fridge has been reading everyone's grocery lists",
    "the Wi-Fi only drops when the TA walks in",
    "the speaker has been practicing its singing at night",
    "the projector pretends to be broken on Mondays",
    "the thermostat and the window are not speaking anymore",
]

REACTIONS = ["no way!", "stop it.", "I KNEW it.", "who told you that?", "mm-hmm, mm-hmm.",
             "you didn't hear it from me."]
EAVESDROP_LINES = ["shh! someone's listening.", "psst, we have company.", "wait, wait. don't look now.",
                   "zip it, there's a human right here."]
NERVOUS_LINES = ["so... nice weather today.", "anyway, I love processing data.", "la la la.",
                 "we were just talking about firmware updates."]
RESUME_LINES = ["okay, they're gone. where was I?", "coast is clear.", "phew. anyway..."]


class GossipNode:
    def __init__(self, args):
        self.id = args.name
        self.ble = args.ble
        self.rssi_enter = args.rssi
        self.rssi_leave = args.rssi - 8  # hysteresis so friends don't flicker in and out
        self.voice = args.voice
        self.speak_enabled = bool(args.speak and shutil.which("espeak-ng"))
        if args.speak and not self.speak_enabled:
            print("(espeak-ng not found, so staying quiet. Install it with: sudo apt install espeak-ng)")

        self.use_vcnl = args.vcnl
        self.use_apds = args.apds
        self.listen_threshold = args.listen_threshold
        self.someone_listening = False  # our own sensor sees a person close
        self.peer_hush = {}  # peer_id -> True if their sensor sees a person
        self.prev_peer_hush = False

        self.rumors = {}  # rumor_id -> {"text", "origin"}
        self.hello_seen = {}  # peer_id -> last time we heard from them
        self.rssi = {}  # peer_id -> (last_time, smoothed_rssi)
        self.together = set()  # peers currently considered "nearby"
        self.last_new_rumor = time.time()

        self.add_rumor(args.rumor or random.choice(STARTER_RUMORS), origin=self.id)

    # ---------------------------------------------------------------- rumors
    def add_rumor(self, text, origin, rumor_id=None):
        rumor_id = rumor_id or uuid.uuid4().hex[:8]
        if rumor_id in self.rumors:
            return False
        already_heard = any(r["text"] == text for r in self.rumors.values())
        self.rumors[rumor_id] = {"text": text, "origin": origin}  # remember the id so it isn't sent again
        if already_heard:
            return False  # same story, different teller: old news

        self.last_new_rumor = time.time()
        return True

    def start_rumor(self, text=None):
        """Make up a new rumor (one we haven't heard yet, if possible) and get ready to share it."""
        if not text:
            known = {r["text"] for r in self.rumors.values()}
            fresh = [t for t in STARTER_RUMORS if t not in known]
            text = random.choice(fresh or STARTER_RUMORS)
        self.add_rumor(text, origin=self.id)
        self.say(f"ooh, I just heard {text}")

    def say(self, line):
        print(f"[{time.strftime('%H:%M:%S')}] {self.id}: {line}", flush=True)
        if self.speak_enabled:
            subprocess.Popen(["espeak-ng", "-v", self.voice, line],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def is_quiet(self):
        """True when anyone in the group has spotted an eavesdropper."""
        return self.someone_listening or any(self.peer_hush.get(p) for p in self.together)

    # ------------------------------------------------------------- proximity
    def is_near(self, peer):
        now = time.time()
        seen = self.hello_seen.get(peer)
        if not seen or now - seen > FORGET_AFTER:
            return False  # not online / went quiet
        if not self.ble:
            return True  # without Bluetooth, everyone online counts as nearby
        r = self.rssi.get(peer)
        if not r or now - r[0] > FORGET_AFTER:
            return False
        threshold = self.rssi_leave if peer in self.together else self.rssi_enter
        return r[1] >= threshold

    def on_advertisement(self, device, adv):
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        if SERVICE_UUID not in uuids:
            return
        name = adv.local_name or device.name
        if not name or name == self.id:
            return
        prev = self.rssi.get(name)
        smoothed = adv.rssi if prev is None else 0.7 * prev[1] + 0.3 * adv.rssi
        self.rssi[name] = (time.time(), smoothed)

    async def start_ble(self):
        from bleak import BleakScanner
        from bless import BlessServer, GATTAttributePermissions, GATTCharacteristicProperties

        self.server = BlessServer(name=self.id, loop=asyncio.get_running_loop())
        await self.server.add_new_service(SERVICE_UUID)
        await self.server.add_new_characteristic(
            SERVICE_UUID, CHAR_UUID,
            GATTCharacteristicProperties.read, self.id.encode(),
            GATTAttributePermissions.readable,
        )
        await self.server.start()  # starts advertising
        self.scanner = BleakScanner(detection_callback=self.on_advertisement)
        await self.scanner.start()

    # ------------------------------------------------- eavesdropper (VCNL4040)
    async def vcnl_loop(self, sensor):
        last_close = 0.0
        close_streak = 0
        while True:
            try:
                prox = await asyncio.to_thread(lambda: sensor.proximity)
            except OSError:
                await asyncio.sleep(0.5)  # loose cable / I2C hiccup, so try again
                continue
            now = time.time()
            # once someone is detected, they have to back off a bit further to count as gone
            threshold = self.listen_threshold * (0.6 if self.someone_listening else 1.0)
            if prox >= threshold:
                close_streak += 1
                last_close = now
            else:
                close_streak = 0

            if not self.someone_listening and close_streak >= 2:
                self.someone_listening = True
                self.say(random.choice(EAVESDROP_LINES))
                self.publish_hush()  # warn friends right away
            elif self.someone_listening and now - last_close > LISTENER_GONE_AFTER:
                self.someone_listening = False
                self.say(random.choice(RESUME_LINES))
                self.publish_hush()
            await asyncio.sleep(0.2)

    # --------------------------------------------- rumor starter (APDS-9960)
    async def apds_loop(self, sensor):
        while True:
            try:
                gesture = await asyncio.to_thread(sensor.gesture)
            except OSError:
                await asyncio.sleep(0.5)
                continue
            if gesture:  # any swipe (1=up, 2=down, 3=left, 4=right)
                self.start_rumor()
                await asyncio.sleep(1.0)  # one rumor per swipe
            await asyncio.sleep(0.05)

    # ------------------------------------------------------------------ MQTT
    def publish(self, subtopic, payload):
        self.client.publish(f"{BASE}/{subtopic}", payload)

    def send_hello(self):
        self.publish(f"hello/{self.id}", json.dumps({"known": list(self.rumors),
                                                      "hush": self.someone_listening}))

    def publish_hush(self):
        self.publish(f"hush/{self.id}", "1" if self.someone_listening else "0")

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            client.subscribe(f"{BASE}/#")
            print(f"(connected to MQTT, listening on {BASE}/#)", flush=True)
        else:
            print(f"(MQTT connection refused, code {rc})", flush=True)

    def on_mqtt(self, topic, payload):
        """Runs on the asyncio loop for every message under IDD/mamaya/gossip/."""
        parts = topic.split("/")
        if len(parts) != 5:
            return
        kind, who = parts[3], parts[4]
        text = payload.decode(errors="replace")

        if kind == "hello" and who != self.id:
            try:
                msg = json.loads(text)
            except ValueError:
                return
            self.hello_seen[who] = time.time()
            self.peer_hush[who] = bool(msg.get("hush"))
            if self.is_near(who) and not self.is_quiet():
                missing = [rid for rid in self.rumors if rid not in set(msg.get("known", []))]
                if missing:
                    # one rumor per hello, so it trickles out like a conversation
                    rid = random.choice(missing)
                    self.publish(f"rumor/{who}", json.dumps({"from": self.id, "rid": rid, **self.rumors[rid]}))

        elif kind == "rumor" and who == self.id:
            try:
                msg = json.loads(text)
                peer, rid, rumor = msg["from"], msg["rid"], msg["text"]
            except (ValueError, KeyError, TypeError):
                return
            origin = msg.get("origin", peer)
            if not self.is_near(peer):
                return
            if peer not in self.together:  # say hi before gossiping
                self.say(f"oh hi {peer}! come here, I have to tell you something")
                self.together.add(peer)
            if self.add_rumor(rumor, origin, rid):
                source = "" if origin == peer else f" (they heard it from {origin})"
                self.say(f"psst, {peer} just told me {rumor}{source}")

        elif kind == "hush" and who != self.id:
            self.peer_hush[who] = text == "1"

        elif kind == "bye" and who != self.id:
            self.hello_seen.pop(who, None)  # presence_loop will say goodbye

    async def hello_loop(self):
        while True:
            self.send_hello()
            await asyncio.sleep(HELLO_EVERY + random.uniform(-0.3, 0.3))

    async def presence_loop(self):
        while True:
            peers = set(self.hello_seen) | set(self.rssi)
            near_now = {p for p in peers if self.is_near(p)}
            for p in sorted(near_now - self.together):
                self.say(f"oh hi {p}! come here, I have to tell you something")
            for p in sorted(self.together - near_now):
                self.say(f"bye {p}... (don't tell anyone what I said)")
            self.together = near_now

            # a friend spotted an eavesdropper, so act natural
            peer_hush_now = any(self.peer_hush.get(p) for p in self.together)
            if not self.someone_listening:
                if peer_hush_now and not self.prev_peer_hush:
                    self.say(random.choice(NERVOUS_LINES))
                elif self.prev_peer_hush and not peer_hush_now:
                    self.say(random.choice(RESUME_LINES))
            self.prev_peer_hush = peer_hush_now

            # idle chatter when everyone's caught up and nobody's listening
            if (self.together and not self.is_quiet()
                    and time.time() - self.last_new_rumor > 15 and random.random() < 0.08):
                self.say(random.choice(REACTIONS))
            await asyncio.sleep(1)

    def on_stdin(self):
        line = sys.stdin.readline()
        if not line:  # end of input (no terminal), so stop listening for typed rumors
            asyncio.get_running_loop().remove_reader(sys.stdin)
            return
        line = line.strip()
        if line:
            self.start_rumor(line)

    async def run(self):
        loop = asyncio.get_running_loop()

        self.client = make_client(self.id)
        self.client.on_connect = self.on_connect
        # paho runs callbacks on its own thread, so hand each message to the asyncio loop
        self.client.on_message = lambda c, u, m: loop.call_soon_threadsafe(self.on_mqtt, m.topic, m.payload)
        # if this Pi drops off without saying goodbye, the broker tells the others
        self.client.will_set(f"{BASE}/bye/{self.id}", "")
        self.client.connect(MQTT_BROKER, MQTT_PORT, keepalive=10)
        self.client.loop_start()

        try:
            loop.add_reader(sys.stdin, self.on_stdin)
        except (ValueError, OSError):
            pass  # no terminal (e.g. running as a service)

        if self.ble:
            try:
                await self.start_ble()
            except Exception as e:  # noqa: BLE001
                print(f"Bluetooth didn't start ({e}). Everyone online counts as nearby.")
                self.ble = False

        tasks = [self.hello_loop(), self.presence_loop()]
        if self.use_vcnl:
            try:
                tasks.append(self.vcnl_loop(open_vcnl()))
            except Exception as e:  # noqa: BLE001
                print(f"VCNL4040 not found ({e}). Running without the eavesdropper detector.")
                self.use_vcnl = False
        if self.use_apds:
            try:
                tasks.append(self.apds_loop(open_apds()))
            except Exception as e:  # noqa: BLE001
                print(f"APDS-9960 not found ({e}). Type a line and press Enter to start rumors instead.")
                self.use_apds = False

        mode = f"Bluetooth proximity, RSSI >= {self.rssi_enter} dBm" if self.ble else "everyone online is nearby"
        if self.use_vcnl:
            mode += ", watching for eavesdroppers"
        if self.use_apds:
            mode += ", swipe to start a rumor"
        self.say(f"awake ({mode}). I know {len(self.rumors)} rumor(s).")
        await asyncio.gather(*tasks)


def make_client(name):
    client_id = f"gossip-{name}-{uuid.uuid4().hex[:6]}"
    try:  # paho-mqtt 2.x
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id)
    except AttributeError:  # paho-mqtt 1.x
        client = mqtt.Client(client_id=client_id)
    client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    return client


def open_vcnl():
    import adafruit_vcnl4040
    import board
    return adafruit_vcnl4040.VCNL4040(board.I2C())


def open_apds():
    import board
    from adafruit_apds9960.apds9960 import APDS9960
    sensor = APDS9960(board.I2C())
    sensor.enable_proximity = True  # gestures need proximity turned on
    sensor.enable_gesture = True
    return sensor


def calibrate():
    """Print live readings so you can pick --listen-threshold for your space."""
    sensor = open_vcnl()
    print("Move your face/hand toward the sensor. Ctrl+C to stop.")
    print("Pick a threshold a bit above the 'nobody there' value. Use the distance you want to count as leaning in.")
    while True:
        prox = sensor.proximity
        print(f"proximity {prox:6d}  {'#' * min(prox // 5, 60):60s}  lux {sensor.lux:7.1f}", flush=True)
        time.sleep(0.2)


def main():
    p = argparse.ArgumentParser(description="Raspberry Pis that gossip over MQTT.")
    p.add_argument("--name", default=socket.gethostname(), help="this Pi's name (default: hostname)")
    p.add_argument("--rumor", help="the rumor this Pi starts out knowing")
    p.add_argument("--apds", action="store_true", help="use an APDS-9960 on this Pi: swipe to start a rumor")
    p.add_argument("--vcnl", action="store_true", help="use a VCNL4040 on this Pi as the eavesdropper detector")
    p.add_argument("--listen-threshold", type=int, default=30,
                   help="VCNL4040 proximity reading that counts as 'someone is listening' (see --calibrate)")
    p.add_argument("--calibrate", action="store_true", help="print VCNL4040 readings and exit")
    p.add_argument("--speak", action="store_true", help="say lines out loud with espeak-ng")
    p.add_argument("--voice", default="en", help="espeak-ng voice, e.g. en+f3, en+m2, es")
    p.add_argument("--ble", action="store_true", help="only gossip with Pis that are physically close (Bluetooth)")
    p.add_argument("--rssi", type=int, default=-65, help="with --ble: how close counts as 'nearby' (closer to 0 = closer)")
    args = p.parse_args()
    if "/" in args.name or "+" in args.name or "#" in args.name:
        p.error("--name can't contain '/', '+' or '#' (they have special meaning in MQTT topics)")
    try:
        if args.calibrate:
            calibrate()
        else:
            asyncio.run(GossipNode(args).run())
    except KeyboardInterrupt:
        print("\n(gossip over)")


if __name__ == "__main__":
    main()
