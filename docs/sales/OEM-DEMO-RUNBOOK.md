# OEM demo runbook — AERON COMPRESSOR SYSTEMS

**Who this is for:** Ash, presenting to a prospect. Ten minutes, click by click.

**The story you are telling:** a machine builder sells a compressor. The compressor
goes into somebody else's factory. The builder wants to see its own machine. The
factory owner decides exactly what the builder may see, and can take it back.
AMP is the layer that makes both sides safe.

**The rule for the whole demo:** everything on screen is real software doing real
work against a real database. The only thing that is fictional is the company.
Say that once, early, and then never claim anything the screen is not showing.

---

## BEFORE THE MEETING

### What must be running

Three things, in this order. Give yourself 15 minutes.

**1. The backend.** Open a terminal, and run:

```
cd C:\Users\ashwi\AMP\backend
venv\Scripts\activate
python -m uvicorn main:app --port 8000
```

Leave it running. Title this window **AMP BACKEND**.

**2. The frontend.** A second terminal:

```
cd C:\Users\ashwi\AMP\frontend
npm run dev
```

Leave it running. The app is at **http://localhost:3000**.

**3. A third terminal for the demo tools.** Title it **AERON GATEWAY**. You will
press Enter in it once, during minute 7. Set it up now:

```
cd C:\Users\ashwi\AMP\backend
venv\Scripts\activate
set DEMO_PASSWORD=your-demo-password-here
```

Also set this one, in the **AMP BACKEND** window *before* you start uvicorn — it
decides what web address the claim QR code points at. Without it the code on
screen will point at the live production site instead of your laptop:

```
set APP_BASE_URL=http://localhost:3000
```

**Two browser windows, not one.** The manufacturer and the factory are two
different logins, and a browser can only hold one AMP session at a time. So:

- **Window 1 — normal Chrome window.** This is NORTHGATE, the customer.
- **Window 2 — an Incognito window.** This is AERON, the manufacturer.

Keep the Incognito window open all day. Closing it loses the AERON session.

> **If you are demoing on the live site (app.marx8.com) instead of your laptop:**
> the flow is identical, but the reset and telemetry commands have to run against
> the production database, not your local one. If you are not certain which
> database your terminal is pointed at, do not run the reset. Demo locally.

### Which demo accounts are used

Everything below is created by the demo seeder. All four use the same password,
whatever you set as `DEMO_PASSWORD`.

| Who | Username | Where they sign in |
|---|---|---|
| The customer's administrator | `demo_factory_admin` | the normal sign-in page |
| The customer's operator (not used, exists for realism) | `demo_factory_operator` | the normal sign-in page |
| AERON's administrator | `aeron_admin` | the same sign-in page |
| AERON's field engineer (not used) | `aeron_engineer` | the same sign-in page |

The demo workspace is **DEMO_AERON**, trading as "Northgate Precision (DEMO)".
The manufacturer is **AERON**, trading as "Aeron Compressor Systems".

**All four accounts sign in at the same page.** You type the username and
password into the ordinary sign-in box and AMP works out which kind of account
it is — a factory user lands on the shop-floor dashboard, a manufacturer lands
on the AERON portal. There is nothing to configure and no separate URL.

(This is worth one sentence to the prospect if they ask: a username belongs to
either a factory or a manufacturer and never both, so there is no ambiguity
about who is signing in.)

**Sign AERON in during prep**, in the Incognito window, so the portal is already
open when you get to minute 1. The session lasts 4 hours from that moment, so do
it within four hours of the meeting.

### How to reset the demo

One command, in the **AERON GATEWAY** terminal:

```
python demo_aeron.py --reset
```

This deletes and rebuilds only the demo: the AERON manufacturer, its ACX-75
catalogue entry, the DEMO_AERON workspace, its four logins, and three machines on
the customer's floor (`COMP-PLANT-A`, `PRESS-01`, `CNC-03`). It refuses to run
unless the workspace it is pointed at is named `DEMO_`, and every delete it makes
is filtered to that workspace or to the AERON manufacturer code — so it cannot
touch a real customer.

It deliberately does **not** create SN-ACX-0001. Registering that machine is
minute 2 of the demo — that is the point of the demo.

**Resetting the demo on the live site instead.** If you are demoing from
`app.marx8.com` rather than your laptop, you cannot run that command — there is
no terminal on the server. Use the variable instead, in the Railway dashboard:

1. Set `DEMO_PASSWORD` once, to whatever you want the four demo logins to use.
   The demo will not build without it, and there is no default — a demo login on
   the public internet with a guessable password is a real account.
2. Set `RESEED_DEMO_OEM` to **a value you have not used before**. The date is the
   obvious choice: `2026-08-14`.
3. Redeploy. The demo rebuilds once during boot.

**Why a new value each time.** Each distinct value is used exactly once and then
recorded, so a variable left set after a meeting cannot quietly rebuild the demo
on every future deploy. That exact mistake wiped the production factory about
forty-one times in one day before the guard existed. If you set the same value
twice, nothing happens — which is the safe direction.

You do not need to reset before *every* live-site demo. Do it when the last
demo was completed, or when `--status` (or the portal) shows SN-ACX-0001 already
registered.

Run the reset **after** every practice run and **before** every real meeting.
A half-walked demo from yesterday is the most common reason a live one goes
strange. Resetting also restarts the demo workspace's trial clock — see failure
1 for why that matters if you have not reset in a month.

After a reset, the AERON session usually still works: the reset recreates the
same usernames, and a token names the user, not the database row. Sign AERON in
again anyway — it takes two seconds and removes the question.

### How to verify it is ready

Run this, in the AERON GATEWAY terminal:

```
python demo_aeron.py --status
```

You want to see:

```
manufacturer         : AERON present
demo factory machines: 3
registered machines  : 0
claims               : 0

READY FOR A DEMO
```

If it says **NOT at the starting state**, run `--reset` again.

Then, by eye, two more checks:

- **Incognito window** shows the AERON portal: the word **Aeron** top left, a
  blue square with an **A**, and five tiles all reading **0**. Under "Installed
  fleet" it says *"No installations recorded for this manufacturer yet."*
- **Normal window** at `http://localhost:3000/login`, signed in as
  `demo_factory_admin`, shows the dashboard. In the left sidebar under **Core
  MES** there is **Connected Equipment**. Click it: it should say *"No
  manufacturer-connected equipment."*

Both of those sentences are your starting line. If either window shows a machine
already, you have not reset.

---

## DEMO — 10 MINUTES

### Minute 0–1 — the problem you are solving

**CLICK:** Nothing. Leave the screen on the AERON portal (the near-empty one) and
talk to the prospect.

**SHOW:** An empty manufacturer portal. Nothing to explain yet — that is
deliberate, and it is the last time the screen will be quiet.

**SAY:**
> "You build machines. Once one leaves your factory it goes dark. You find out
> how it is running when somebody rings you, usually because it has already
> stopped. Everyone in your position wants the same thing — see your own machines
> in the field, know when they are due a service, sell the service contract.
>
> The reason it does not happen is not technical. It is that your customer will
> not give a supplier a login to their factory system. And they are right not to.
> Their order book, their costs, their recipes — none of that is your business,
> and if the only way to get you hours data is to hand you a login, the answer is
> no.
>
> What I am going to show you takes ten minutes. It is one compressor, from the
> day you build it to the day your customer changes their mind about what you can
> see. Everything on screen is the real product. The only invented thing is the
> company name."

**DO NOT SAY:**
- *"Every machine builder has this problem"* — you do not know their fleet, their
  contracts, or whether they already have a telematics product. It invites the
  answer "actually we already have that", and you have spent your opening on a
  claim you cannot support.
- *"AMP is an IoT platform"* — AMP is a manufacturing system that has an
  equipment-manufacturer layer on it. Calling it an IoT platform sets the
  expectation that it ships gateways and device management. It does not.
- *"This is running on real machines at a customer site"* — it is a demo database
  on your laptop. Say that once, up front, and you never have to defend it later.

---

### Minute 1–2 — the manufacturer's own portal

**CLICK:** Point at the top-left of the AERON window (already signed in).

**SHOW:** The header reads **Aeron**, with a blue tile and the line *"Connected
equipment, powered by AMP · aeron_admin · OEM_ADMIN"*.

**SAY:**
> "This is Aeron's portal, not their customer's. Different login, different table
> of users, different screen. Aeron's name and their brand colour come from
> configuration — this is one piece of software, not a version we forked for
> them. A logo goes in the same way; we have not set one here."

**DO NOT SAY:**
- *"Your engineers log in on the same page as your customers do"* — they do not.
  Factory and manufacturer accounts are separate tables with separate sign-in
  routes, and the manufacturer sign-in page has not been built yet. Claiming a
  shared front door is a promise the product would fail at on day one.
- *"It's fully white-labelled, they'd see their own domain"* — name, colour and
  logo are configurable. A custom domain per manufacturer is not built; the claim
  link address is one setting for the whole deployment, not one per manufacturer.

**CLICK:** Point at the five tiles across the top.

**SHOW:** Machines 0 · Active 0 · Reporting 0 (*"seen in the last 48 h"*) ·
Silent 0 (*"reported, then stopped"*) · Not shared 0 (*"customer has not shared
health"*).

**SAY:**
> "Five numbers. The one I want you to notice is the last one — 'not shared'.
> That is separate from 'silent' on purpose. A machine whose owner has chosen not
> to share its health is not a broken machine, and if a system counts it as one
> you send an engineer three hours up the motorway for nothing. AMP will not do
> that. Right now every number is zero, because Aeron has not built anything yet."

**DO NOT SAY:**
- *"Reporting means the machine is online right now"* — it means a message
  arrived within the last 48 hours. There is no live connection state.
- *"We monitor uptime"* — AMP records what was reported. It does not poll a
  machine, and it cannot tell "switched off" from "network down".
- *"'Not shared' always means the customer refused"* — that tile counts every
  machine AMP has no last-seen time for, which includes a machine that has simply
  never reported yet. In this demo SN-ACX-0001 sits in that tile between minute 5
  and minute 7 even though Northgate did share health. The honest reading of the
  tile is "we do not know", not "they said no".

---

### Minute 2–3 — building the machine

**CLICK:** Scroll to **Machines and installations**. Click the **Serial number**
box and type:

```
SN-ACX-0001
```

**SHOW:** The serial in the box; the **Model** dropdown beside it.

**SAY:**
> "Aeron have just finished a compressor on their own shop floor. Before it goes
> on the lorry, they register it. This is Aeron's record of their own machine —
> nothing to do with any customer yet."

**CLICK:** Open the **Model** dropdown and choose **ACX-75 — Aeron ACX-75 rotary
screw compressor**.

**SHOW:** The model selected.

**SAY:**
> "The model comes from Aeron's own catalogue. Sitting behind that one line is the
> thing that makes all of this work for any manufacturer, not just compressor
> people: a list of what an ACX-75 reports. Twelve signals — discharge pressure,
> discharge temperature, motor temperature, running hours, loaded hours, kilowatts,
> alarm code, and so on. Aeron's own tag names, mapped to AMP's names. If you
> build presses, you send us your list and nothing in the product changes."

**DO NOT SAY:**
- *"You can set your models up yourself in the portal"* — there is no screen for
  creating a model in this build, and no API route that creates one either. A
  manufacturer's catalogue is loaded during onboarding. Say "we set your catalogue
  up with you when you come on", which is true.
- *"AMP understands compressors"* — it understands nothing about compressors.
  The vocabulary is data supplied by the manufacturer. That is the stronger
  claim anyway, so make it.

**CLICK:** In **Warranty start**, pick **today's date**. In **Warranty end**,
pick the **same date two years out**.

> Both are marked *(optional)* and they genuinely are — skip them and everything
> still works, the machine simply reads "no warranty recorded" everywhere. Type
> them: it is what a real manufacturer does, and it makes the customer's
> acceptance screen show the cover they are getting.

**SHOW:** The two dates in their boxes.

**SAY:**
> "Two years' cover from despatch. Aeron type it once, here, and every screen
> after this one — theirs and their customer's — reads it off this record. Notice
> AMP did not fill it in for them. The model says twenty-four months, and AMP
> still will not turn that into a date, because it does not know whether your
> cover runs from the day it leaves your yard or the day it is commissioned. That
> is your commercial decision and it stays yours."

**CLICK:** Press **Register and create code**.

**SHOW:** A blue panel appears: **Installation code for SN-ACX-0001**.

**SAY:**
> "One press did two things. It registered the machine — and it made an invitation."

---

### Minute 3–4 — the code that travels with the machine

**CLICK:** Point at the code in the blue panel, in the format
`AMP-XXXXX-XXXXX-XXXXX`.

**SHOW:** The panel says *"Print this with the machine or send it to the
customer. AMP stores only a hash — this is the only time it can be shown."*
Below it, a line reading **QR target: http://localhost:3000/claim/AMP-…**.

**SAY:**
> "That code goes on the machine. On the nameplate, on the paperwork, in the
> crate. Fifteen characters, and we have taken out every letter you can misread
> off an oily label — no I, no L, no O, no U, no zero, no one. Somebody can read
> it down the phone and it still works.
>
> AMP does not keep the code. We keep a one-way fingerprint of it and the last
> four characters, so if your customer rings up we can match their sticker to our
> record — but nobody at Aeron, and nobody at AMP, can ever read that code back
> out. This panel is the only time it exists. That is why it will not go away
> until I dismiss it.
>
> The address underneath is what a QR code on the machine would point at. Your
> label printer makes the QR; AMP supplies the address."

**DO NOT SAY:**
- *"AMP prints your QR labels"* — AMP produces the web address. It does not
  generate or render a QR image anywhere in the product.
- *"Scanning the code adds the machine"* — it does not. Scanning opens a page
  with the code filled in. The lookup is a separate press and the acceptance is
  another one, precisely so a forwarded link or a passing phone cannot attach
  equipment to somebody's account.
- *"We can resend the code if they lose it"* — you cannot resend it. You can
  withdraw the old invitation and issue a new one. Say that instead; it is a
  better answer and it is true.

**CLICK:** Select the code with the mouse and copy it (Ctrl+C). Then click
**I have saved it**.

**SHOW:** The panel closes. The table below now has a row:
`SN-ACX-0001 · ACX-75 · Pending · …<last four> · not yet · <a date 30 days out>`,
with a **Withdraw** button on the end.

**SAY:**
> "Now it is a pending invitation. Aeron can see they have offered it and that
> nobody has taken it yet. It expires in thirty days, and if the machine goes to
> a different customer, they press Withdraw and the code is dead."

**DO NOT SAY:**
- *"You'd be notified the moment they accept"* — there are no emails, texts or
  push notifications to a manufacturer in this build. AMP does file a notification
  record for the manufacturer when a claim is accepted, but the portal has no
  screen that shows it, so what actually happens on screen is that the row's
  status changes the next time the page is loaded. Say "it shows here the next
  time you look", which is what they will see.

---

### Minute 4–5 — the machine arrives at the customer

**CLICK:** Switch to the **normal Chrome window** — the customer, signed in as
`demo_factory_admin`. In the left sidebar under **Core MES**, click **Connected
Equipment**.

**SHOW:** The heading **Connected Equipment**, the line *"Machines on your shop
floor that came from an equipment manufacturer, and exactly what each manufacturer
can see about them"*, and below it *"No manufacturer-connected equipment."*

**SAY:**
> "Different company, different login, different system entirely as far as they
> are concerned. This is Northgate — Aeron's customer. Notice what is on their
> screen right now: nothing. Aeron registered a machine two minutes ago and it has
> not appeared here. That is the most important design decision in the whole
> product. A supplier cannot put a row on your screen. If they could, anyone could
> post equipment into your account and invite you to grant them access to it."

**CLICK:** Click **Add equipment**. Click into the **Claim code** box and paste
the code (Ctrl+V).

**SHOW:** The code in the box, with the note *"Scanning the QR on the machine
opens this page with the code filled in. Dashes and capitals do not matter."*

**SAY:**
> "The compressor has arrived. Their maintenance manager has the code off the
> machine — typed, or scanned, either way."

**CLICK:** Press **Look up machine**.

**SHOW:** A panel: **You are about to add** — `SN-ACX-0001`. Manufacturer: Aeron
Compressor Systems. Model: Aeron ACX-75 rotary screw compressor. Warranty:
*expires* and the date you typed two minutes ago. And the line *"If this is not
the machine in front of you, do not add it."*

**SAY:**
> "Still nothing has been added. That was a look, not a decision. They can see who
> made it, which model, and the cover Aeron recorded — before they accept it, not
> after. If the serial on the screen is not the serial on the crate, they stop
> here."

> If you skipped the warranty dates at registration, this line reads *"no warranty
> end date recorded for this installation"* instead. That is not a fault and it is
> worth saying out loud if it happens: AMP reports what it was told and does not
> invent a period. But the demo is stronger with the dates in.

**DO NOT SAY:**
- *"It's already connected to their system"* — nothing is attached until the next
  press. Saying it is connected now undercuts the exact point you just made.
- *"AMP checks the machine is genuine"* — AMP checks that a valid, unspent,
  unexpired code was presented. It cannot verify hardware.
- *"If they type it wrong it tells them what's wrong"* — every failure gives the
  same sentence on purpose: mistyped, expired, withdrawn, already used. Telling
  them apart would help somebody guessing codes.

---

### Minute 5–6 — the customer decides what Aeron may see

**CLICK:** Point at the section headed **What Aeron Compressor Systems may see**.

**SHOW:** Seven tick boxes, all empty:
*Machine health score and connectivity state* · *Operating and loaded hours* ·
*Service due / overdue status* · *Equipment alarm codes raised by this machine* ·
*Live telemetry readings from this machine* · *Maintenance work carried out on
this machine* · *Downtime events recorded against this machine*.
Underneath: *"You are sharing nothing. The machine will still be added."*

**SAY:**
> "Here is the part your customers will care about, and it is the part that gets
> you through their procurement.
>
> Seven things, all off. Not a slider, not 'basic / advanced', not a contract
> clause — seven named things, in English, that they tick one at a time. And read
> the line at the bottom: they can add the machine and share nothing at all. That
> is a complete, valid answer, and the product does not sulk about it."

**CLICK:** Tick three boxes: **Machine health score and connectivity state**,
**Operating and loaded hours**, **Service due / overdue status**.

**SHOW:** The line below changes to *"Sharing 3 of 7. No setting here can reveal
work orders, production quantities, recipes, inventory, costs or operators."*

**SAY:**
> "They have given Aeron the three things a compressor supplier actually needs to
> service the machine. Not alarms, not the raw telemetry, not their maintenance
> records. And the sentence underneath is the one that ends the argument: there is
> no combination of these seven boxes that reveals a work order, a production
> quantity, a recipe, a cost or an operator's name. Those are not behind a
> permission — they are not reachable from the manufacturer side at all."

**DO NOT SAY:**
- *"It's granular per machine"* — it is not. The agreement is per manufacturer,
  per workspace. If Aeron have four machines at Northgate, these seven boxes
  cover all four. Saying per-machine would be a promise the next demo breaks.
- *"We're SOC 2 certified"* / *"ISO 27001"* / *"it's GDPR compliant"* — AMP holds
  no certification. What you can say is: the separation is enforced in the code
  and proved by an adversarial test suite, and you are happy to walk their IT
  people through it.
- *"They can see an audit trail of everything Aeron looked at"* — every *change*
  to sharing is audited with before-and-after. Individual read requests are not
  logged and are not presented back to the customer as a viewing log.

**CLICK:** Press **Confirm and add machine**.

**SHOW:** The panel closes. A card appears headed **Aeron Compressor Systems**,
*AERON · 1 machine here*, *Currently shares 3 of 7*, with the same seven tick
boxes — three now blue — and beneath them *"Not shared: Equipment alarm codes
raised by this machine, Live telemetry readings from this machine, Maintenance
work carried out on this machine, Downtime events recorded against this machine."*
Below it, a table **Equipment on site** with the row for `SN-ACX-0001`.

**SAY:**
> "Now it is theirs, and the choice they just made has become a permanent control
> on their own screen. Look at what the product does here: it lists what they are
> *not* sharing just as plainly as what they are. A screen that shows you three
> green ticks and quietly leaves out the other four reads as 'we share
> everything'. This one refuses to let you misread it."

---

### Minute 6–7 — connecting it to the actual machine on the floor

**CLICK:** In the **Equipment on site** table, find the **Machine on floor**
column for `SN-ACX-0001`. It reads **Not linked**, with a grey note beneath:
*"needed before its maker can commission it"*. Open that dropdown and choose
**COMP-PLANT-A**.

**SHOW:** The dropdown now reads COMP-PLANT-A and the grey note disappears. The
Lifecycle column reads **Assigned**; Warranty reads **unknown**; Service reads
**unknown**.

**SAY:**
> "One more step, and it is the customer's, not the supplier's. Aeron know the
> serial number they built. Only Northgate know which asset on their floor is
> carrying it — the compressor by the door in Plant A. So Northgate make that
> connection. Aeron cannot, and that is on purpose: a supplier that could point at
> any machine in your factory could inherit that machine's data and call it their
> own telemetry."

**DO NOT SAY:**
- *"That link means the machine is now connected"* — nothing has been received.
  It is a piece of paperwork saying which asset is which.
- *"The service position will fill in shortly"* — it says "unknown" because no
  hours have ever been reported, and it stays unknown until a real reading
  arrives. Do not promise it. (The warranty is already there — Aeron typed it at
  registration — so only the service half is blank at this point.)

**CLICK:** Switch to the **Incognito (AERON)** window and press **F5** to
refresh. Click the row for `SN-ACX-0001` in the **Installed fleet** table.

**SHOW:** A panel slides in from the right, headed `SN-ACX-0001`. Under
**Commissioning · incomplete** there are four lines:
✓ *The installation names a customer and site* — DEMO_AERON ·
✓ *The serial is linked to a machine in that factory* ·
✓ *The model defines what this machine reports* — 12 signals ·
✗ *The machine has reported at least once* — **never reported**.

**SAY:**
> "This is Aeron's commissioning checklist, and it is not a form somebody ticks —
> every line is a fact the system can check for itself. Customer named: yes.
> Linked to an asset on the floor: yes. We know what an ACX-75 reports: yes.
> Has this machine ever actually said anything: no. Not once. So the honest answer
> is that it is not commissioned, and the checklist says which one of the four is
> missing rather than just failing."

**DO NOT SAY:**
- *"Now I press Commission and we're live"* — there is no Commission button in the
  portal in this build. Commissioning exists in the interface our software talks
  to, and the button is on the list. What is on screen is the readiness checklist,
  which is what you should describe.
- *"AMP configures the compressor's controller"* — AMP configures nothing on any
  machine. It reads what is sent to it.
- *"We ship you a gateway / edge box"* — there is no edge device programme. No
  device certificates, no provisioning, no over-the-air updates. Do not imply one.

---

### Minute 7–8 — the machine speaks

**CLICK:** Switch to the terminal window titled **AERON GATEWAY** and press
**Enter** on the pre-typed line:

```
python demo_aeron.py --telemetry
```

**SHOW:** The terminal prints `published flowmes/DEMO_AERON/-/machines` and the
readings. Switch back to the **Incognito (AERON)** window and press **F5**.

**SAY:**
> "That is one report from the compressor. In a real installation it comes off the
> machine's controller, through the customer's own gateway, over MQTT — the
> standard messaging protocol for this kind of equipment — onto a topic that names
> which factory it belongs to. I am sending it from this laptop because there is no
> compressor in this room, but it goes through exactly the same code that a real
> gateway's message would."

**SHOW:** In the fleet table, the **Hours** column for `SN-ACX-0001` changes from
*no data* to **3960 h**. The tiles read Machines 1 · Reporting 1. Open the machine
row again: the fourth commissioning line has turned ✓, and under **Telemetry** you
can see the twelve signal names the model defines — running, loaded, unloaded,
discharge_pressure, discharge_temperature, motor_temperature, operating_hours,
loaded_hours, power_kw, energy_kwh, alarm_code, dryer_status.

**SAY:**
> "Three thousand nine hundred and sixty hours. Aeron did not tell us that number
> and neither did we — the machine reported it, under Aeron's own tag name, and
> the model's profile is what told us it meant running hours. The commissioning
> check has gone green because the fact it was waiting for finally happened."

**DO NOT SAY:**
- *"This is live data from a real compressor"* — it is a message this laptop sent.
  Say "one report, sent from here, through the real path" — which is stronger,
  because it is checkable.
- *"And the status went green because of the message"* — the **Status** column read
  *Running* before the report as well; it comes from the machine row on the
  customer's floor, which the seeder created as Running. The hours are the thing
  this message produced. Point at the hours.
- *"AMP speaks OPC UA / Modbus / Profinet / EtherNet-IP to your machines"* —
  **it does not.** The only working way telemetry enters AMP is MQTT. There is a
  screen elsewhere in the product listing six industrial protocols; those are
  demonstration rows whose values are generated by a simulator, not working
  connections to a PLC — and that screen says so itself.
  If they ask about their PLC protocol, the true answer is: "your gateway or a
  converter speaks to the PLC and publishes MQTT to us — that is the integration
  work, and we'd scope it with you."
- *"It streams continuously"* — each message is one report. There is nothing in
  the product that guarantees an interval.
- *"It buffers and catches up if the site loses internet"* — there is no
  store-and-forward, no offline capability, nothing on the machine end at all.
  That capability does not exist and promising it is the kind of thing a pilot
  dies on.
- *"You can put a machine on a plant called 'Plant 1'"* — site names travel inside
  the message address and cannot contain spaces. Be careful how you answer this
  one: sites exist in the data model and CSV onboarding sets them, but a machine
  created through the API has no site and is addressed with a `-`, and there is no
  field today for setting one through the API. If they ask about multi-site, say
  the naming has rules and an onboarding step you will hand their integrator, and
  do not promise a screen for it.

---

### Minute 8–9 — what Aeron does with it

**CLICK:** In the AERON window, scroll to **Service queue**.

**SHOW:** The heading, and directly beneath it: *"Arithmetic over what each
machine reported. Nothing here is a prediction, and nothing here uses a
customer's production data."* Below, two items:

- amber **medium** · `SN-ACX-0001` · *This machine is within 5% of its service
  interval.* — evidence *"3960.0 h run, never serviced, 4000 h interval, 40.0 h to
  the next service"* — → *Schedule a service visit*

One item, not a wall of them. If you skipped the warranty dates at registration
there is a second, grey **low** line — *No warranty period is recorded for this
installation* — which is AMP asking Aeron for something only Aeron knows.

**SAY:**
> "This is the commercial bit. Forty hours to a service, on a machine three
> hundred miles away, that nobody rang anybody about. That is a service visit you
> can schedule, a van you can route, and a contract you can renew before it lapses.
>
> Now read the line under the heading, because I want to be straight with you.
> This is arithmetic. Four thousand hour interval, three thousand nine hundred and
> sixty on the clock, never serviced, forty to go. That is a subtraction, and the
> product tells you it is a subtraction. It is not predicting a failure and there
> is no model behind it.
>
> And notice what is not here. One machine, one line. AMP is not padding this
> screen with a row per compressor to look busy — the healthy ones are silent,
> which is the only way a queue like this survives contact with a real fleet."

**DO NOT SAY:**
- *"This is predictive maintenance"* — **it is not.** No model was trained,
  nothing is predicted. It is a subtraction over reported hours, and the product
  says so on screen. If you claim prediction, the prospect's engineer will ask
  what it was trained on and there is no answer.
- *"It's AI-powered"* — no AI, no machine learning, no model of any kind is
  involved in the service queue.
- *"It's 94% confident"* — nothing here carries a confidence number, and no screen
  in the product shows one today. There is code for a straight-line projection
  from an hours counter — it needs at least three readings spanning at least a day,
  caps itself at 85% and labels itself "a straight line through an hours counter,
  not a model" — but nothing in the running product feeds it that history, so it
  never fires. Do not invent a percentage, and do not promise the projection
  either.
- *"It knows the machine is about to fail"* — it knows the hours counter passed a
  threshold their own model defines.

**CLICK (optional, if the room is engaged):** In the AERON GATEWAY terminal, run:

```
python demo_aeron.py --telemetry --hours=4085
```

Then refresh the AERON window.

**SHOW:** The queue item turns red **high**: *This machine has passed its service
interval.* — *"4085.0 h run, never serviced, 4000 h interval, -85.0 h to the next
service"*.

**SAY:**
> "Same machine, a few more days of running. Now it is overdue and it has gone to
> the top. Nobody at Northgate had to tell Aeron, and nobody at Aeron had to ring
> Northgate."

---

### Minute 9–10 — the customer changes their mind

**CLICK:** Switch to the **normal (Northgate)** window, still on **Connected
Equipment**. In the **Aeron Compressor Systems** card, untick **Operating and
loaded hours**. Press **Save sharing**.

**SHOW:** Green text: *"Saved. This takes effect immediately."* The line below now
reads *"Not shared: Operating and loaded hours, Equipment alarm codes…"*

**SAY:**
> "Northgate have changed their mind about one thing. Not the relationship — one
> field. Their administrator, on their own screen, in two clicks. They do not ring
> us and they do not ring Aeron."

**CLICK:** Switch to the **Incognito (AERON)** window and press **F5**.

**SHOW:** In the fleet table, the **Hours** column for `SN-ACX-0001` now reads
**not shared**. The **Status** column still reads **Running**. Click the row: the
panel says *"This customer has not shared: alarms, downtime, maintenance history,
operating hours, telemetry. Those fields are blank because permission was not
given, not because the machine is faulty."*

**SHOW — and this is the half worth pointing at:** scroll up to the **Service
queue** on the same page. The machine is **still listed as overdue** — the top
line still reads *"This machine has passed its service interval."*, because that
permission is still on. But the grey line under it no longer reads *"4120.0 h
run, never serviced, 4000 h interval…"*; it now reads *"the operating-hour
figures are not shared by this customer"*.

Open the machine row again if you want the fuller sentence: the **Service** line
in the panel reads *"past its service interval (every 4000 h); the operating-hour
figures are not shared by this customer"*. The **4000** is Aeron's own number,
off Aeron's own model, so it stays. The **4120** was Northgate's, and it is gone
from every line on the screen.

**SAY:**
> "Gone. On the very next request Aeron made — not tonight, not on the next sync,
> the next request. Permission is checked every single time the question is asked,
> so there is no copy of yesterday's data sitting somewhere that outlives the
> permission that allowed it.
>
> And look at what it says instead of the number. It says *not shared*. It does
> not say zero hours. If it said zero, Aeron's service desk would book an engineer
> against a number that nobody ever gave them. The machine is still there, the
> status is still there, because that permission is still on. One field went, and
> only that field.
>
> And notice the service queue did not empty out. Aeron are still told this
> machine is past due — Northgate agreed to that, and it is the thing Aeron
> actually needs in order to send somebody. What Aeron lost is the reading. They
> know to come; they no longer know the number. That is a permission doing real
> work, not an on-off switch.
>
> That is the whole pitch. Your customer stays in control, in a way they can see
> and undo in two clicks — and because they can, they will actually say yes."

**DO NOT SAY:**
- *"Aeron have been locked out"* — they have not. They still see the machine, its
  model, its serial, their own warranty record, and the two categories still
  granted. Overstating this makes the fine-grained control you just demonstrated
  sound like an on/off switch.
- *"The data has been deleted"* — nothing was deleted. The customer's data was
  never copied to Aeron in the first place; the question simply stops being
  answered.
- *"We purge their cache"* — there is no cache to purge. Permissions are read at
  the moment of the request, which is *why* withdrawal is instant. That is the
  better sentence.
- *"Every customer gets this and it's certified"* — the behaviour is real and
  tested; there is no external certification behind it. If they push, offer the
  engineering write-up rather than inventing a badge.

---

## IF SOMETHING GOES WRONG

### 1. The sign-in fails, or a window has lost its session

**What you will see:** "Invalid username" on the sign-in page, or the AERON
window bouncing you back to the sign-in screen, or the AERON portal saying
*"This is the manufacturer portal — your session is a factory session."*

**Most likely causes:**
- the two sessions have collided because both windows are the same Chrome
  profile;
- the four-hour session expired;
- or, if the message is *"Trial expired — contact your provider to activate your
  subscription"*, the demo workspace was last rebuilt more than thirty days ago.
  The demo tenant is created on a trial that runs from the moment of the reset,
  so `python demo_aeron.py --reset` clears this. It is one more reason to reset
  before every meeting rather than after.

**What to do:** sign in again as `aeron_admin` in the Incognito window.
That is a two-second fix. If the *normal* window is the broken one, sign in again
as `demo_factory_admin`.

**SAY, while you fix it:**
> "That is my fault, not the product's — I have got the manufacturer and the
> customer logged in on the same machine, which is not how it works in real life.
> Two seconds."

**DO NOT SAY:** *"It's a known bug"* — it is not a bug, it is you running two
identities on one laptop. Do not hand the prospect a defect that does not exist.

### 2. The machine will not register — "Serial SN-ACX-0001 is already registered"

**Most likely cause:** you did not reset after your last practice run.

**What to do, live:** just use the next serial. Type `SN-ACX-0002` instead and
carry on — nothing else in the demo changes. Do **not** try to reset mid-meeting:
the reset deletes the manufacturer, the workspace, the invitation and any machine
you have just created, so the story you are halfway through disappears and every
row on screen goes stale.

**SAY:**
> "That serial is from the last one of these I ran — serials are unique per
> manufacturer, which is exactly what you'd want. I'll take the next one off the
> line."

**DO NOT SAY:** *"Serials are globally unique across the platform"* — they are
not, and deliberately so: two manufacturers can both ship an SN-001 and they are
different machines. A global namespace would let one manufacturer discover
another's serial numbers by probing for clashes.

### 3. The telemetry step does nothing — the hours stay blank

**What you will see:** you press Enter in the AERON GATEWAY terminal, refresh the
portal, and Hours still reads "no data"; or the terminal prints
`SN-ACX-0001 does not exist yet`.

**Three causes, in order of likelihood:**
1. You have not linked the machine on the floor yet (minute 6–7). Telemetry only
   reaches the installation record once the customer has said which asset it is.
   Go and do the link, then press Enter again.
2. You registered a different serial (see failure 2). The telemetry command looks
   for `SN-ACX-0001` specifically.
3. The backend terminal is not running, or the terminal you pressed Enter in is
   pointed at a different database.

**What to do:** fix the link, re-run, refresh. If it still does not appear, do not
fight it — move on to minute 8–9 and talk through the service queue from the
Connected Equipment screen on the customer's side instead, where the warranty and
service states are already visible.

**SAY:**
> "The link between the serial and the asset on the floor is what lets the report
> land — that is the step I just did on the customer's screen, and it is
> deliberately the customer's step, not the supplier's. Let me do that in the
> right order."

**DO NOT SAY:**
- *"The gateway must have dropped out"* — there is no gateway. Do not invent a
  hardware failure to cover a demo mistake; if they take it seriously you will
  spend the next ten minutes on a device story that does not exist.
- *"It'll catch up in a minute"* — it will not. Nothing retries, nothing buffers,
  and there is no background job that will fill it in.
