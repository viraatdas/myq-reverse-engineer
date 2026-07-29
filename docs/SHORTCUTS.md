# iOS Shortcuts setup

Everything here assumes you have your API URL and key. Throughout, replace:

- `YOUR-API-URL` → e.g. `https://abc123.execute-api.us-east-1.amazonaws.com`
- `YOUR_KEY` → the `API_KEY` from your `.env`

> **Why the key goes in the URL.** Shortcuts *can* set headers, but a URL-only
> action is one tap to build and works identically in automations, on the Home
> Screen, and in Siri. The connection is HTTPS, so the key is encrypted in
> transit. If you would rather use a header, every example works with
> `X-API-Key` instead — see [Using a header instead](#using-a-header-instead).

---

## 1. The simplest possible shortcut

**Shortcuts → + → Add Action → Get Contents of URL**

URL:
```
https://YOUR-API-URL/open?key=YOUR_KEY
```

That's the whole shortcut. Name it "Open Garage". It now works from the
Shortcuts app, the Home Screen, the Lock Screen, the Action Button, and Siri
("Hey Siri, Open Garage").

Make a second one for `/close`.

> `Get Contents of URL` defaults to GET, and this API accepts GET on every
> command specifically so you never have to change the method.

---

## 2. A shortcut that tells you what happened

The basic version returns immediately — before the door has actually moved. To
confirm the door finished, add `wait=true` and `format=text`:

**Action 1 — Get Contents of URL**
```
https://YOUR-API-URL/close?key=YOUR_KEY&wait=true&format=text
```

**Action 2 — Show Notification**
Set the body to the *Contents of URL* variable.

You'll get a notification reading **"Garage Door is closed"** — confirmed, not
assumed. Swap Show Notification for **Speak Text** if you want it read aloud.

`wait=true` polls the door for up to 25 seconds. If the door is still moving
when that runs out you get a 504; pass `&timeout=28` to stretch it (API Gateway
caps any single request at 30 seconds).

---

## 3. Check the door without touching it

**Get Contents of URL** → `https://YOUR-API-URL/state?key=YOUR_KEY`

`/state` returns a single bare word — `open`, `closed`, `opening`, or
`closing` — which makes it usable directly in an **If** comparison with no JSON
parsing:

```
Get Contents of URL   https://YOUR-API-URL/state?key=YOUR_KEY
If  [Contents of URL]  is  "open"
    Show Notification  "Garage is still open!"
Otherwise
    Show Notification  "Garage is closed."
End If
```

Use **If → Text → is** and type `open` as the comparison value.

---

## 4. Automation: close the garage when you leave home

**Shortcuts → Automation → + → Leave**

1. Choose your home address, set a radius, choose **Immediately After I Leave**
2. **Next** → **New Blank Automation**
3. Add **Get Contents of URL**:
   ```
   https://YOUR-API-URL/close?key=YOUR_KEY&wait=true&format=text
   ```
4. Add **Show Notification** with the *Contents of URL* variable
5. Turn **Ask Before Running** OFF, and **Notify When Run** ON

Because `/close` is idempotent, this is safe if the automation fires more than
once — a door that's already closed returns "already closed" without sending a
redundant command.

Leaving the notification on is worth it: it's how you find out the door *didn't*
close.

---

## 5. Automation: open the garage when your car connects

This is the Tesla/CarPlay Bluetooth trigger.

**Shortcuts → Automation → + → Bluetooth**

1. Choose your car from the device list, set **Is Connected**
2. **Next** → **New Blank Automation**
3. Add **Get Contents of URL**:
   ```
   https://YOUR-API-URL/open?key=YOUR_KEY
   ```
4. Turn **Ask Before Running** OFF

### Only open when actually near home

Bluetooth connects wherever you are, so gate it on location. Put these before
the URL action:

```
Get Current Location
Get Distance from [Current Location] to [Home Address]
If  [Distance]  is less than  0.2
    Get Contents of URL   https://YOUR-API-URL/open?key=YOUR_KEY
End If
```

Set the distance units to miles (or use `0.3` for km). Without this check, your
garage opens every time you start the car anywhere in the world.

---

## 6. Automation: warn me if the garage is open at night

**Automation → + → Time of Day → 10:00 PM → Daily**

```
Get Contents of URL   https://YOUR-API-URL/state?key=YOUR_KEY
If  [Contents of URL]  is  "open"
    Show Notification  "Garage is still open"
    Get Contents of URL   https://YOUR-API-URL/close?key=YOUR_KEY&wait=true&format=text
    Show Notification  [Contents of URL]
End If
```

Turn **Ask Before Running** OFF if you want it to close automatically, or leave
it ON so it prompts you first.

---

## 7. A toggle button for the Home Screen

**Get Contents of URL** → `https://YOUR-API-URL/toggle?key=YOUR_KEY&format=text`
→ **Show Notification** with *Contents of URL*

Then **Share → Add to Home Screen**. One tap opens or closes, whichever applies.

---

## Using a header instead

If you'd prefer the key not appear in a URL, in **Get Contents of URL** tap
**Show More** and set:

- **Method**: GET
- **Headers**: key `X-API-Key`, value `YOUR_KEY`

and drop `?key=YOUR_KEY` from the URL. Everything else is unchanged.

---

## Troubleshooting

**"The operation couldn't be completed" / no response**
Test the URL in Safari on the same phone. If Safari works and Shortcuts doesn't,
the automation likely has **Ask Before Running** on and is waiting for a tap.

**Shortcut runs but nothing happens**
You probably got an error body instead of a success. Add **Show Notification**
with *Contents of URL* to see the actual message, or add `&format=text`.

**`Invalid or missing API key`**
The key is wrong, or the URL has a stray space. Rebuild the URL by pasting it
whole rather than typing it.

**`The garage door opener is offline`**
The opener has lost its connection to MyQ. Check the MyQ app itself — nothing
here can command an offline opener.

**`MyQ authentication expired`**
Run `python -m myq.cli setup` on your computer to log in again.

**Automation fires but the door was already moving**
Expected. Commands are idempotent — a door that's already closing won't be sent
another close command unless you pass `&force=true`.

---

## Reference

| Want | URL |
|---|---|
| Open | `/open?key=KEY` |
| Close | `/close?key=KEY` |
| Toggle | `/toggle?key=KEY` |
| Open, confirmed, spoken | `/open?key=KEY&wait=true&format=text` |
| State for an If block | `/state?key=KEY` |
| Full status as JSON | `/status?key=KEY` |
| Specific door | add `&serial=SERIAL` |
| Force a redundant command | add `&force=true` |
