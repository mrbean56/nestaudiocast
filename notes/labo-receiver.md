# Lab note — task C: our own Cast receiver, and the rate-trim probe

Worktree `~/opencode/nestaudiocast-wt/receiver`, branch `feat/receiver`, 2026-10-25 21:35–22:20.
Authoring host CT 118 `opencode` (192.168.2.18), device **Chromecast Salon** 192.168.2.80,
receiver page served from CT 121 (192.168.2.21:7640).

Rule of the house: hypothesis → measurement → conclusion, and a number that is not in `evidence/`
does not exist. Every section below says what was measured, what was not, and why.

**Summary of what this note establishes**

| | what | state |
|---|---|---|
| C0 | the device answers, is idle, and **does not list `28028CD2`** (`APP_UNAVAILABLE`) | measured |
| (a) | codec matrix: H.264 plays; HEVC, VP9 (mp4 and webm), AV1 are **rejected** | measured **on the platform player**, not on our page |
| (b) | `playbackRate` **is honoured** on this Chromecast: 0.998 → −0.001868 ± 0.000136, 1.002 → +0.001936 ± 0.000139 | measured **on the platform player**, not on our page |
| (c) | a rate change does **not** stall: player stays `PLAYING`, no playhead backstep | measured on the platform player |
| (d) | clock: **not measured** — needs our namespace | unproven |
| (e) | start dispersion: **not measured** — needs our `play_at` | unproven |
| — | the page's protocol logic, executed offline with a stubbed CAF | 33/33 checks pass |

The caveat is the same in (a)–(c): our page could not be launched tonight (C0), so its own numbers
do not exist. What was measured is the Chromecast's **own** video player on the same device — the
same platform media pipeline, not our HTML5 page.

---

## C0 — preflight: the device is fine, our unpublished app is not listed

**Hypothesis.** `.80` answers read-only probes, is idle on Backdrop, and lists `28028CD2` as an
available app, so launching it is safe and the page can be exercised.

**Measurement.** `./tools/receiver_probe.py --probe-only` → `evidence/41-preflight.json`: name
`Chromecast Salon`, mDNS model `Chromecast`, capabilities `201221`, cast build `1.56.291998`,
uptime 61 494 s, TCP 8008/8009 reachable, app `Backdrop` (`E8C28D3C`), `isIdleScreen: true`,
volume `level 0.25, muted false`. So far the hypothesis holds.

Then, on the same read-only connection, `GET_APP_AVAILABILITY`:

```
28028CD2 (ours)      -> APP_UNAVAILABLE
CC1AD845 (DMR)       -> APP_AVAILABLE
E8C28D3C (Backdrop)  -> APP_AVAILABLE
233637DE (YouTube)   -> APP_AVAILABLE
```

and `LAUNCH 28028CD2` answers `LAUNCH_ERROR / NOT_FOUND`; the device stays on Backdrop
(`evidence/41-preflight-unavailable.json`). `/setup/eureka_info` answers with `model_name: null`
on this firmware, hence the model from the mDNS `md` field.

One attempt to make the device re-fetch its app list was made, because that is the last step of
the documented procedure (`notes/fake-cast-device.md` §3.1 step 7: wait 15 min, power-cycle):
`POST /setup/reboot` → **403** (the endpoint is authenticated on this firmware), uptime kept
counting (61 494 → 61 726 s), so **the device was not rebooted**. No further attempt: I will not
reach for a physical action from here.

**Conclusion.** The Cast stack is healthy; the **app** is missing. An unpublished receiver only
appears on a device whose serial number is registered in the Cast console, 15 min after saving
and a power cycle. So this is an owner-side action, not a code problem: either the serial of
*Chromecast Salon* is not the one registered, or the registration has not propagated. I report
"the receiver page could not be exercised tonight" and deliver everything else. Nothing was
faked: there is no (d) and no (e) number, and that is said out loud.

**What I did instead.** The device *does* have a video app that is available: the Default Media
Receiver (`CC1AD845`). Task C's decisive question — *does this Chromecast honour
`playbackRate`?* — is a property of the platform's media pipeline, and it can be measured there
with exactly the method `evidence/17` used on the group. That is the fallback below, labelled
`-dmr` in every file so nobody can mistake it for a measurement of our page.
`./tools/receiver_probe.py --backend dmr` (the default `--backend auto` refuses our page and
chooses this path when `GET_APP_AVAILABILITY` says `APP_UNAVAILABLE`).

---

## (a) Codec matrix — what actually plays

**Hypothesis.** The device plays H.264; HEVC/VP9/AV1 are unknown, and `canPlayType` is not
evidence (it is a claim, not a measurement).

**Measurement.** `./tools/receiver_probe.py --only a --backend dmr` →
`evidence/41a-codec-matrix-dmr.json`. Six-second, **video-only** assets generated on CT 121
(`--make-assets`), served from `:7640/media/`, loaded one at a time with `autoplay`, 9 s of
observation each. Verdicts say *rejected* (not "no") when the player accepts the `LOAD`, reports
`LOADING`, then drops the media session — that is the signature of an unsupported stream, and it
is a different fact from a network failure.

| asset | container / codec | verdict | evidence in the file |
|---|---|---|---|
| `codec-h264.mp4` | MP4 / H.264 Baseline | **plays** | reaches `PLAYING`, playhead advances **6.00 s**, `videoInfo 640x360 sdr` |
| `codec-hevc.mp4` | MP4 / HEVC Main (`hvc1`) | **rejected** | `LOAD`→`LOADING`, session gone after **3.0 s**, never `PLAYING` |
| `codec-vp9.mp4` | MP4 / VP9 profile 0 | **rejected** | same, session dropped |
| `codec-vp9.webm` | WebM / VP9 profile 0 | **rejected** | same (so it is not a container problem) |
| `codec-av1.mp4` | MP4 / AV1 main (SVT-AV1) | **rejected** | same |

`supportedMediaCommands` on every `MEDIA_STATUS` is `274447` — the same mask the group reported
in `evidence/17`, and it *does* claim the playback-rate bit (274447 & 4096 = 4096). See (b).

**Conclusion.** Only H.264 is usable tonight. The contract's "best `avc1` ≤ `MAX_HEIGHT` plus
`mp4a`" (§4) is therefore not just a preference, it is the only path that works on this hardware;
HEVC/VP9/AV1 must not be offered as a fallback. **Not proven:** the same matrix on *our* page (its
`hello` carries the `canPlayType` claims, `codecs`, but we could not compare claims to reality —
that comparison is the point of the test and it never ran).

---

## (b) Rate trim — the number that decides v0.2's architecture

**Hypothesis** (from `evidence/17`, group): `SET_PLAYBACK_RATE` is a no-op on the audio side. Open
question: is it a no-op on the **video** side of our own receiver too? If yes, v0.2 has no actuator
and its acceptance cannot be met; the integrator must be told before anyone writes a workaround.

**Measurement.** `./tools/receiver_probe.py --only bc --backend dmr` →
`evidence/41b-rate-trim-dmr.json`. Method identical to `probe/rate_gain.py` / `evidence/17`:
60 s of playhead samples per rate, least squares of media time against wall time, standard error
from the residuals; the `SEEK` to 0 before each window guarantees 60 s of media ahead. 600 polls
per window at 10 Hz. Asset: `rate-90s.mp4` (H.264, 90 s, 2.9 MB, generated on CT 121).

| asked | measured ratio | σ | measured gain | asked gain | error | distinguishable from 0 at 3σ |
|---|---|---|---|---|---|---|
| 1.000 | **0.999824** | 0.000140 | −0.000176 | 0 | −1.25σ | no (correctly) |
| 0.998 | **0.998089** | 0.000138 | **−0.001911** | −0.002 | +0.64σ | **yes (13.8σ)** |
| 1.002 | **1.002044** | 0.000140 | **+0.002044** | +0.002 | +0.31σ | **yes (14.6σ)** |

Archived run: 592–600 playing samples per window, 59.9 s span, residual max 147–168 ms (that is
the stale-read noise above, not a rate error). Two earlier full runs of the same measurement gave
0.998 → −0.001868 / −0.002089 and 1.002 → +0.001936 / +0.001988 (σ 0.000136–0.000139 each): three
runs, means **−0.001956 ± 0.000067** and **+0.001989 ± 0.000031**, i.e. the applied gain sits
4e-5 from what was asked, which is under one standard error of a single run. The lever is
reproducible, not a one-off.

For scale: a 0.2 % rate lever is **120 ms of playhead per minute**, i.e. the loop can absorb a
full ±20 ms digital-error budget in 10 s, and a 10-minute drift of −12 ms (`evidence/17`) is
nothing. `RATE_MAX=0.002` in §6 is a sensible ceiling: it is inside what this device honours.

**Conclusion.** On this Chromecast the video side **does honour `playbackRate`**, with a gain
error below 0.02 % (≈1σ of the measurement) at a resolution of 1.4e-4. That is the opposite of the
group, and it is what v0.2's "group leads, video follows through rate trims" design needs.
**The caveat that must not be lost:** this was measured through the platform's own player, not
through `receiver.html`. Our page sets `video.playbackRate` on the media element inside the CAF
shell; the platform's `SET_PLAYBACK_RATE` reaches the same pipeline, but "same pipeline" is an
argument, not a measurement. Until the app is registered, (b) is **proven for the device, unproven
for our page**.

---

## (c) What a rate change costs

**Hypothesis.** A rate change might rebuffer, drop frames, or step the playhead back — which would
make it unusable as a steady-state actuator ("corrections are rate trims, never reloads", §1).

**Measurement.** Same run, `evidence/41c-rate-stall-dmr.json`: a 3 s band of 20 Hz polls around
each `SET_PLAYBACK_RATE`.

| asked | states seen in the band | left `PLAYING` | playhead backstep | max poll gap |
|---|---|---|---|---|
| 1.000 | `PLAYING` only | **no** | 0.000 s | 90 ms |
| 0.998 | `PLAYING` only | **no** | 0.000 s | 101 ms |
| 1.002 | `PLAYING` only | **no** | 0.000 s | 90 ms |

No `BUFFERING`, no `IDLE`, no `LOAD`/`SEEK` echo: the change is applied in place. The poll gaps are
the 10–20 Hz cadence, not stalls.

**Conclusion.** On the platform player a rate change is free: no rebuffer, no playhead step, no
dropped-frame counter available to say otherwise. **Not measured:** `buffered_end` and `dropped`
around a change — the media namespace does not expose them; only our own `state` message does,
which is why the page reports exactly those two fields. That part of (c) is unproven.

One measurement note for whoever builds the controller: single `GET_STATUS` reads of the playhead
are **stale by up to 144–159 ms** (max residual of the least-squares fit), exactly the sawtooth
`notes/00-labo.md` A20 found on the TV. The slope is unaffected (30 min of samples average it), but
a controller that reacts to one sample will chase noise. This is the same lesson as A15/A20 and it
is now measured on this device too.

---

## (d) Clock — not measured (unproven, because…)

`./tools/receiver_probe.py --only d` sends 200 `clock` pings at 5 Hz and reports RTT median/p95
plus the residual of the offset estimator. It talks to **our namespace**, which exists only when
`receiver.html` runs. The app is not listed on the device (C0), so the ping has no receiver and
the number does not exist. The tool implements it and the page answers it (`clock` → `clock_reply`
with `rx_ms` sampled in the same statement as nothing else), but **no number is claimed**.

What is *not* blocked: the page's own clock convention is statically verified
(`tests/v020/test_receiver_page.py`: `performance.timeOrigin + performance.now()`, and `rx_ms` and
`currentTime` are read in the **same statement**, as §3 demands), and the whole protocol surface —
`clock`/`clock_reply`, `load`, `play_at`, `pause`, `seek`, `rate`, `stop`, `idle`, `hello`, `state`,
`intent`, `ended`, `error` — is **executed offline** against a stub CAF:
`node receiver/selftest.js` → **33/33 checks pass** (`evidence/41y-page-selftest.json`). That includes
the two behaviours this section and the next depend on: `clock` answers with the sender's `seq` and
`state` carries `rx_ms`, `media`, `rate`, `player_state`, `buffered_end`, `dropped`. It is not a
simulator and proves nothing about the device; it does prove the page is not dead code.

## (e) Start dispersion — not measured (unproven, because…)

`./tools/receiver_probe.py --only e` performs ten `play_at` at a target position and reports
`err = media(first playing sample) − [target_media + (rx_ms − target_rx)/1000·rate]`, i.e. the
video-side landing σ the controller needs for its one allowed re-landing (§1). Same blocker: it
needs `play_at` on our page. **No number is claimed.** The probe, the page handler (park paused,
then a **single** `play()` at the computed wall time — not a burst of seeks) and the analysis are
in place and will run unchanged once the app is available.

For scale, the v0.1 numbers it must be compared with: PAUSE→SEEK→PLAY σ 30 ms, LOAD σ 202 ms
(`evidence/27`). A video-side landing inside ~30 ms would make `DELTA_V2` calibration meaningful.

---

## (f) State of the device when the run ends

`evidence/41z-final-state.json`: `left_on_backdrop: true`, app `Backdrop`, volume
`level 0.25, muted false` — identical to the preflight (`volume_before`), and the mute the probe
set for its own duration was undone (`volume_restored`). The device was never set to level 0
(hard rule), and nothing was ever played on the Cuisine group or on the TV: the probe only ever
talks to `.80`, and every asset is video-only, so even an attached HDMI cable would stay silent.

---

## Exact commands (reproduce all of it)

```sh
cd ~/opencode/nestaudiocast-wt/receiver

# 0. the page must be served at the frozen LAN address, on :7640 only
./tools/receiver_serve.sh start          # deploy receiver/ + stdlib http.server on 192.168.2.21:7640
./tools/receiver_serve.sh status         # pid, listener, /healthz, /receiver.html, cast-relay state
./tools/receiver_serve.sh stop           # nothing left behind; :8099 never touched

# 1. preflight only — launches nothing, plays nothing
./.venv/bin/python tools/receiver_probe.py --probe-only          # -> evidence/41-preflight.json

# 2. our app: refused by the device tonight, verbatim
./.venv/bin/python tools/receiver_probe.py --backend receiver    # exit 6, prints APP_UNAVAILABLE

# 3. the measurements that could run (platform player as stand-in), ~5 min
./.venv/bin/python tools/receiver_probe.py --make-assets         # ffmpeg on CT 121 -> /opt/nac-dev/media
./.venv/bin/python tools/receiver_probe.py --only a --backend dmr    # -> evidence/41a-codec-matrix-dmr.json
./.venv/bin/python tools/receiver_probe.py --only bc --backend dmr   # -> 41b-rate-trim-dmr.json, 41c-rate-stall-dmr.json
./.venv/bin/python tools/receiver_probe.py --backend dmr         # all of the above + 41z-final-state.json

# 4. the measurements that need the app (both will work unchanged once it is listed)
./.venv/bin/python tools/receiver_probe.py --only d --backend receiver   # 200 clock pings at 5 Hz
./.venv/bin/python tools/receiver_probe.py --only e --backend receiver   # 10 x play_at

# 5. static honesty of the page, and the safety suite
./tests/v020/run.sh
./tools/prove.sh --fast
```

`--make-assets` needs no local ffmpeg: it runs `ffmpeg` on CT 121 over SSH and writes 4.7 MB of
video-only test media to `/opt/nac-dev/media` (CT 121 has 2.9 GB free; nothing is deleted).

## What blocks (a)–(e) on the page, and what would unblock it

One owner-side action: in the Cast console, make sure the **serial number of the Chromecast Salon**
is the device registered for `28028CD2`, with the receiver URL `http://192.168.2.21:7640/receiver.html`,
then wait 15 min and power-cycle it. The probe then reports `APP_AVAILABLE` and `--backend auto`
picks our page without any change to the code; (d) and (e) produce their files on the first run.
A read-only way to check afterwards, before launching anything:

```sh
./.venv/bin/python tools/receiver_probe.py --probe-only | grep -A3 app_availability
```

## Contradiction with the contract

None on behaviour, one on shape: §3.1's `play_at` has only `rx_ms` and `media`, and §3.2's `state`
is measured with `buffered_end` and `dropped` which the platform's media namespace does not expose
at all. The page reports both; the DMR stand-in could not, which is why (c) is only partly proven.
I am **not** proposing an amendment for that — it strengthens §3 as written.

One small thing the integrator may want to freeze: the receiver echoes the **controller's** `seq`
on replies, and stamps unsolicited messages (`state`, `ended`, `error`) with the last `seq` it saw,
which is the only coherent reading of "the receiver never sends a message without `seq`" (§3). It
is implemented that way in `receiver.html` and worth a sentence in the contract.

---

## C5 — L'app tourne enfin : quatre bugs que seule la mesure a montrés (2026-10-26, 15:30–21:10)

`APP_AVAILABLE` obtenu à 15:31 après correction du numéro de série (console) et redémarrage. À
partir de là, tout ce qui suit a été **mesuré sur l'appareil**, en silence (HDMI sur une entrée
éteinte, volume de l'appareil muet, médias **sans piste audio**).

| # | Symptôme mesuré | Cause | Correctif |
|---|---|---|---|
| 1 | `clock_reply` arrive, **`state`/`error`/`ended` jamais** | `broadcast()` passe par CAF `getSenders()`, qui ne liste pas un contrôleur CASTv2 brut | renvoyer aussi au **dernier expéditeur entendu** (`lastSender`) |
| 2 | premier `load` → `no_media_element` | une page custom n'a **pas** de `<video>` (le Styled Receiver en fournit un) | la page crée et possède son élément (`#nac-media`) |
| 3 | `load` à 20 s joue à **0** | `mediaEl()` renvoyait l'élément vide de CAF alors que les événements étaient liés au nôtre | un seul élément piloté = un seul élément écouté (`dashMode` explicite) |
| 4 | `play_at` joue depuis la mauvaise position | la libération partait même sans métadonnées | attente bornée puis `metadata_timeout` (dire la vérité plutôt que mentir) |

**Et le coût caché** : l'appareil **met en cache par chemin** et ignore les chaînes de requête
(`/receiver.js?v=A` récupéré une fois, jamais re-essayé pour deux builds suivants), et il ne
rafraîchit tout son bundle qu'au **redémarrage**. Trois redémarrages ont été nécessaires pour
trois corrections de page. Solution retenue : l'URL gelée sert un **chargeur stable** qui lit
`/b/<now>/build.txt` et charge `/r/<build>/receiver.js` — chemins différents à chaque build, donc
plus de redémarrage pour une mise à jour de page. Le service sert ces deux chemins, et aussi les
deux chemins de la première génération (`/build.txt`, `/receiver.js`) pour qu'un appareil qui a
l'ancien chargeur en cache puisse encore démarrer.

## C6 — Le manifeste DASH ne joue sur aucun des deux lecteurs (21:05)

**Hypothèse** : le MPD synthétisé par la tâche B est jouable ; si notre page échoue, c'est notre
usage de CAF.

**Mesure**, session vivante (`tools/receiver_measure.py` + inspection directe) :
- MPD servi par le service, structure correcte : `type="static"`, `SegmentBase indexRange` +
  `Initialization`, `BaseURL` relatifs `0`/`1` ;
- les plages répondent **206** avec `Content-Range` exact ; l'init contient `ftyp`+`moov`, l'index
  commence par un **`sidx`** ; taille totale 2 507 362 728 octets ;
- **notre page** avec `load{content_type: application/dash+xml}` → `player_state: loading`,
  `ready: 0`, `metadata_timeout` au bout de 5 s, aucun `caf_error` ;
- **le DMR de la plateforme** (le même appareil, autre lecteur) chargé sur le même MPD → réponse
  `IDLE`, puis plus aucune session : il refuse aussi.

**Conclusion** : le problème n'est ni notre page ni CAF, c'est **le manifeste** — deux lecteurs
indépendants le refusent. Ce qui reste à établir : quelle contrainte DASH manque (hypothèses non
départagées : `SegmentBase@timescale`, contenu du `sidx` YouTube vs attentes Shaka, `Period`
unique + `startWithSAP`, ou le fait que Shaka veut un `sidx` par représentation avec des offsets
relatifs à la fin du `sidx`).
**Ce que ça ne remet pas en cause** : le proxy et le service des plages sont corrects (206, sha256
identique, reprise après expiration, `evidence/40-*`). C'est le **manifeste** qu'il faut revoir.

**Piste pour la suite (à décider, pas décidée)** : écrire un lecteur MSE minimal dans la page
(init + fragments par plages lues dans le `sidx`, ajoutés à `SourceBuffer`) au lieu de dépendre du
lecteur DASH de CAF. La page possède déjà son élément et `playbackRate` y est honoré (mesuré) ;
cela supprime d'un coup la dépendance au manifeste et permet un seek exact par fragment. Coût :
~150 lignes de JS et son harnais hors ligne.

## C7 — Ce qui a été mesuré ce soir, sur notre page (à comparer à 41a-c, lecteur de la plateforme)

`evidence/41d-page-measure-night.json` :
- **horloge** : 200/200 réponses, RTT médian 10,2 ms (p95 10,7), dispersion de l'offset **σ 0,82 ms**
  sur les 20 meilleurs RTT → l'estimateur de la tâche D tient sur le vrai appareil ;
- **tête de lecture de notre page** : σ **55,9 ms**, p95 106 ms, **sawtooth** — même forme que le
  lecteur de la plateforme (σ 60,1). Autrement dit la page n'est pas plus propre : le filtre
  moyenne 30 s de `sync.VideoClock` est justifié pour les deux ;
- **cadence via la page** : 1,002 → **+0,002003**, 0,998 → **−0,002684** (le 0,998 sort 34 % trop
  fort, le 1,002 à 0,2 %). Mesuré sur le build **avant** le correctif d'élément : valable pour
  l'API de l'élément, à refaire sur le build courant.
- **non mesuré** : dispersion de départ de `play_at` (les 10 essais ont tous mesuré la page
  d'avant le correctif, qui parquait à 0).

## C8 — Le lecteur MSE, et les deux obstacles qu'il reste (2026-10-26, 23:00)

Décision du propriétaire : remplacer la lecture DASH par un lecteur MSE dans la page. Fait
(`receiver/mse.js`, `receiver/mse_selftest.js`) :

- lecture du `sidx` (version 0 et 1, offsets absolus), table de fragments, `appendBuffer` par
  plage, **20 s** d'avance, seek exact par fragment, cadence et pause passées à l'élément que la
  page possède déjà ;
- **harnais hors ligne sur les octets réels** d'une session vivante
  (`tests/v020/fixtures/video-sidx.bin`, 15 940 octets de `sidx`, 1 594 fragments, timescale
  12 800, durée totale **8 630 s** = la vidéo). Il a attrapé un vrai bug : la table donnait le
  premier fragment à **19 160** au lieu de **19 902** (les offsets étaient relatifs au tampon du
  `sidx`, pas au fichier). Après correctif, la table couvre le fichier **exactement** : premier
  fragment à 19 902, dernier octet **2 507 362 727** sur 2 507 362 728.

**Ce qui bloque encore, mesuré** :
1. `addSourceBuffer` → *« This MediaSource has reached the limit of SourceBuffer objects it can
   handle »*. Deux pistes, non départagées : la limite du Chrome 90 embarqué est peut-être **1**
   tampon, ou deux `addSourceBuffer` concurrents se marchent dessus (le code ajoute les pistes en
   parallèle). Test suivant, déjà en place : le journal `add <mime> #n` de `mse.js` dira combien de
   tampons existent et à quel moment le refus tombe. Si la limite est 1, la sortie propre est un
   **deuxième élément** (`<audio>` avec son propre MediaSource), synchronisé sur l'élément vidéo.
2. L'appareil exécute encore un **build de page antérieur** au chargeur « chemins par build » : son
   chargeur en cache demande `/receiver.js?v=<stamp>`, un seul chemin, donc caché pour toujours.
   `hello.build` le prouve (appareil `ceddec214b03`, serveur `3023f08a2eed`). Un redémarrage rend
   le chargeur actuel ; après quoi une mise à jour de page ne coûte plus qu'un relancement.

**Ce qui est déjà prouvé pour le service** : le service sert `/b/<now>/build.txt`,
`/r/<build>/receiver.js` et `/r/<build>/mse.js`, et son `build_stamp` couvre **les deux** fichiers
(sinon un `mse.js` modifié gardait l'ancien chemin).

## C9 — Le lecteur MSE joue (2026-10-27 00:40) et les trois pièges de cache

**Résultat** : avec `receiver/mse.js`, un flux YouTube **joue** sur le Chromecast : `playing`,
`buffered_end` à **23,4 s** vingt secondes après le `load`, tête de lecture qui avance. C'est le
premier son-image de v0.2 obtenu par notre propre code.

Trois contraintes découvertes en chemin, chacune mesurée :
1. **Un seul `SourceBuffer` par `MediaSource`** sur cet appareil : le tampon vidéo s'ajoute, celui
   de l'audio est refusé (« This MediaSource has reached the limit of SourceBuffer objects it can
   handle »). D'où **un élément par piste** (`#nac-media` et `#nac-audio`), chacun avec son
   `MediaSource`, l'audio asservi à l'horloge vidéo (tolérance 60 ms).
2. **Une instance d'app « coincée »** : après plusieurs sessions, l'app est toujours la nôtre mais
   sa page ne répond plus au namespace — chaque partage échouait sur `no hello` jusqu'à arrêt manuel.
   `SenderLink.launch()` donne maintenant **6 s** à une instance adoptée pour dire bonjour, puis
   l'arrête et la relance **une fois** (testé hors ligne avec un faux récepteur qui retient son
   premier `hello`).
3. **Le cache de l'appareil est un labyrinthe** : il met en cache **par chemin**, ignore les chaînes
   de requête, et garde tout (page, `receiver.js`, `build.txt`) jusqu'au redémarrage. Trois
   générations de chargeur ont été nécessaires :
   - `?v=<stamp>` → mort (un seul chemin, jamais re-essayé) ;
   - `/r/<build>/receiver.js` → bon, **sauf** si `build.txt` lui-même est en cache ;
   - le service **redirige** désormais les deux chemins de la première génération
     (`/receiver.js`, `/build.txt`) vers `/r/<stamp>/…`, ce qui débloque un appareil resté sur la
     première génération **sans** redémarrage — sauf si son `build.txt` est en cache, auquel cas un
     dernier redémarrage reste nécessaire.

**Ce qui reste à mesurer dès que l'appareil exécute le build courant** (là, il tourne encore un
build antérieur : `hello.build` = `3023f08a2eed`, serveur = `04321c0419e4`) : dispersion de départ
de `play_at` (10 essais), premier frame après un partage (objectif ≤ 15 s), cadence via MSE, puis
la matrice de transport.

## C10 — Mesures sur l'appareil, page au build courant (2026-10-27 00:45)

Redémarrage fait : `hello.build` = `04321c0419e4` = le tampon du serveur → le chargeur
« chemins par build » fonctionne, et une mise à jour de page ne coûte désormais qu'un relancement.
Tout a été mesuré **en silence** (banc `video_only`, appareil muet au niveau près, médias muets).
`evidence/50-service-bench-night.json` :

| Mesure | Résultat | Critère |
|---|---|---|
| **premier frame** après partage | **4,5 s** (page chaude), **13,1–13,7 s** (relance à froid) | ≤ 15 s → **PASS** |
| **atterrissage de `play_at`** (10 essais) | la page démarre l'élément **112 ms en retard**, σ 35 ms, pire 196 ms | à re-tenter par atterrissage + trim |
| **cadence via la page** | 1,002 → +0,002003 ; 0,998 → −0,002684 | l'élément obéit (le 0,998 sort 34 % fort) |
| **pause / reprise / arrêt / nouveau partage** | vérifiés par `/api/control` | ✓ |
| **seek +600 s** | vérifié deux fois (position 600,0–600,2) | ✓ |
| côté **groupe** | **non mesuré** : le banc est en `video_only`, le groupe n'est jamais chargé | porte audible |

**Trois bugs de plus, trouvés par la mesure** :
1. une **libération `play_at` en attente survivait à une pause** : le lecteur repartait tout seul
   (le service affichait `playing` alors qu'on venait de mettre en pause) → un jeton d'annulation,
   toute commande de transport invalide la libération programmée ;
2. la page **rappelait nos propres seeks comme des intentions utilisateur** (deux `seeked` par
   parquage), donc le contrôleur miroitait nos seeks vers le groupe → suppression par type
   d'événement (`seeked` 2,5 s, `play` 2 s, `pause` 700 ms, 120 ms pour le parquage), une vraie
   pause utilisateur restant rapportée ;
3. **la première mesure d'atterrissage mesurait la valeur parquée** (0,0 ms neuf fois sur neuf) :
   le critère juste compare la position à l'instant demandé **sur l'horloge du récepteur**, et
   seulement une fois la tête de lecture en mouvement.

**Défaut ouvert, mesuré** : le **premier partage après une session précédente** échoue parfois
(3 fois sur 6 essais) — l'app reste lancée mais inactive et la session finit en `receiver_failed`
ou ne démarre pas. Le recyclage ajouté ce soir couvre la forme « pas de hello » ; celle-ci est
différente et reste à instruire.

## C11 — Le « premier partage » qui échoue : cause racine et mesure (2026-10-27 05:10)

**Hypothèse** : le défaut ouvert de C10 (3 échecs sur 6 cycles) est un problème de la page.

**Mesures**, cycles partage → lecture → arrêt contre le service déployé, en silence :

| étape | cycles | réussis | mode d'échec |
|---|---|---|---|
| avant | 6 | **2** | `GET_STATUS` timeout qualifié de prise de contrôle ; puis `stale_app` (sonde de 6 s) |
| après 1 Hz + lookahead 12 s + garde du pump | 8 | 4 | toujours `GET_STATUS` timeout |
| **après réouverture de la liaison** | **10** | **10** | — (premier frame 9,4–9,7 s) |

**Cause racine** : les nouvelles tentatives de `GET_STATUS` se faisaient **sur la même socket**. Or
c'est la socket qui meurt quand l'app redémarre (le Chromecast ferme la session TLS) : chaque
tentative brûlait son propre délai (2 s) sur une liaison morte, soit 6,5 s perdues, et le partage
échouait. La tentative suivante **rouvre la liaison** avant de réessayer.

**Contributions secondaires, toutes mesurées** :
- la propriété de l'appareil était vérifiée **4 fois par seconde** (§3.3 demande « en un poll ») :
  1 Hz suffit et divise la charge par quatre sur un appareil qui décode ;
- la résolution du média et le lancement de l'app étaient **séquentiels** alors qu'ils sont
  indépendants : ils tournent maintenant en parallèle (c'est ce qui ramène le premier frame de
  13,7 s à 9,6 s) ;
- la sonde « hello » de recyclage est passée de 6 s à **3 s** ;
- côté page, un arrêt qui croise un `appendBuffer` levait « This SourceBuffer has been removed »
  et faisait échouer le partage : l'ajout est abandonné proprement, et l'avance de tampon est
  passée de 20 s à 12 s.

**Preuve** : `evidence/51-first-share-flakiness.json`, test de régression
`tests/v020/test_chromecast_net.py::TestStatusOnADeadLink` (le faux récepteur coupe la session et la
liaison doit répondre quand même). **Réserve** : tout cela est le côté vidéo ; le côté groupe de
chaque ligne de transport exige une fenêtre audible.
