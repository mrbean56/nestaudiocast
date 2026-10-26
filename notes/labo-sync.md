# Labo — la loi de commande v0.2 (tâche F), prouvée en simulation

Établi le 2026-10-25. Branche `feat/sync-controller`, worktree `~/opencode/nestaudiocast-wt/sync`.
Aucun appareil n'a été touché : pas de socket, pas d'enceinte, pas de Chromecast, rien d'audible.

> **Ce que ce document est.** La preuve, hors ligne, que la loi de commande décrite au §1–§2 de
> `docs/v020-contracts.md` tient ses critères **contre le bruit réellement mesuré** (`evidence/17`,
> `27`). Ce n'est pas une preuve sur le matériel : rien ici ne dit si le Chromecast honore
> `playbackRate`, ni quelle latence il a. Ces deux chiffres sont la tâche C, et le simulateur les
> accepte en paramètres pour qu'ils puissent être déposés sans réécrire une ligne.

## 0. Résultat en une ligne

Sur 10 minutes simulées avec le bruit mesuré, le médian 5 s de l'erreur numérique reste à
**p95 = 2,5 ms** (cible 10 ms de la tâche ; le contrat, §2 et consensus §8.3, demande 20 ms — les
deux sont tenus), la session converge en **3 s** après le démarrage, et **aucun** `load`, `seek` ni
ré-atterrissage n'est émis une fois le régime établi. Le cas pessimiste (gain de
cadence 0,5, dispersion de départ 40 ms) **échoue visiblement à 87 ms** — il n'est pas masqué.

## 1. Fichiers

| Fichier | Rôle |
|---|---|
| `lib/v020/sync.py` | `Controller` : la loi, pure logique, zéro I/O. Signatures gelées remplies. |
| `tests/v020/sim_v020.py` | le simulateur (bruit mesuré, paramètres en ligne de commande, `--json`) |
| `tests/v020/test_sync.py` | 28 tests unitaires de la loi (plan de départ, signe, bande morte, événements, observations) |
| `tests/v020/test_sync_sim.py` | 17 tests : les critères « done when » vérifiés **sur** la simulation, et le déterminisme |
| `evidence/44-sim.json` | les résultats, régénérables en une commande |

## 2. La loi

### 2.1 La plante

Le groupe mène et **ne peut pas** être ralenti/accéléré (`SET_PLAYBACK_RATE` est un écho sans
effet, `evidence/17`). Le seul actuateur est `playbackRate` du récepteur. Avec
`u = rate − 1` et `e = p_group − p_video − DELTA_V2` :

```
d(p_group)/dt = 1          (le groupe file à son quartz)
d(p_video)/dt = 1 + u      (le récepteur, mesuré par son propre taux rapporté)

  ⇒  ė = −u                (un u positif fait remonter le vidéo, donc réduit e)
```

C'est toute la physique du problème : un intégrateur pur, de gain −1, avec un bruit de mesure de
σ 10,2 ms sur une des deux têtes de lecture. Une erreur de signe sur `u` (ou sur `DELTA_V2`)
transforme cette boucle stable en boucle qui diverge — d'où le §3.

### 2.2 Les deux régimes, jamais mélangés (§1)

| Phase | Commande émise | Pourquoi |
|---|---|---|
| `landing` (démarrage, seek, reprise) | un `group_play{at, media}` planifié + un `play_at{rx_ms, media}` | l'atterrissage fait la convergence ; une seule re-tombée vidéo autorisée |
| `settling` (atterri, < 30 s) | rien | §1 : les trims ne sont permis qu'en régime établi |
| `steady` (≥ 30 s) | `rate` **uniquement** | §1 : plus jamais de rechargement |
| `paused` / `ended` | `group_pause`+`pause` / `group_stop`+`idle` | transport symétrique |

Le contrôle est une assertion, pas une promesse : `step()` lève `AssertionError` si une commande
autre que `rate` sort en phase `steady` (`lib/v020/sync.py`, test
`test_steady_assertion_fires_if_a_reload_ever_leaks_in`).

### 2.3 L'atterrissage (ce qui fait vraiment la convergence)

Le groupe est garé en pause à la position `m`. À l'instant `land`, on veut :

```
p_group(land) = m            p_video(land) = m − DELTA_V2      (invariant §2)
```

- le groupe reçoit `group_play{at: land − lead_s, media: m}` : il lui faut `lead_s` pour repartir,
  et c'est **le seul terme de latence** que la loi doit connaître ;
- le récepteur reçoit `play_at{rx_ms: horloge_récepteur(land), media: m − DELTA_V2}` : il est
  **auto-calé** (§3.1), sa propre latence de démarrage ne sort jamais du récepteur ;
- après 3 s d'observation, si l'erreur estimée dépasse **10 ms** (la cible), **une** re-tombée
  vidéo replace le vidéo sur la position que le groupe *aura* à l'instant visé :
  `media = p_video(land) + ê`. Le groupe n'est pas touché : zéro rechargement, zéro trou.

C'est cette re-tombée qui fait que la dispersion d'atterrissage du groupe (σ 30 ms, `evidence/27` C)
ne coûte rien : elle est mesurée, puis annulée côté vidéo. Elle n'est donc pas « gratuite » pour
autant : elle compte comme un événement de transport, et le régime établi repart 30 s plus tard.

### 2.4 Le régime établi : gains, bande morte, slew

```
ê   = médiane des erreurs brutes des 8 dernières secondes (rejet des lectures périmées)
u   = clamp(K_P·ê + K_I·∫ê , ±RATE_MAX)        seulement si |ê| > 5 ms
slew: |Δu| ≤ 1e-3 par seconde                   (pleine échelle atteinte en 2 s)
∫ê  bornée à |K_I·∫ê| ≤ RATE_MAX/32             (elle annule la dérive, 2,07e-5, rien d'autre)
émission: au plus une toutes les 8 s, et seulement si |Δrate| ≥ 2e-4
```

Les valeurs ne sont pas des goûts, elles se déduisent :

- **`K_P ≤ 0,1`** vient du critère de dérive : « le cas dérive seule ne demande jamais un trim
  au-dessus de `RATE_MAX/4` » ⇒ au bord de la bande morte `K_P·5 ms ≤ 5e-4`. `K_P = 0,04` laisse
  la place au bruit de l'estimateur (~2 ms) sous le même plafond. `K_I = K_P²/4` rend le couple
  critique (ω = 0,02 rad/s) : assez lent pour une estimation à 4 Hz vieille de quelques centaines
  de ms, assez rapide pour la dérive mesurée.
- **La fenêtre de 8 s** n'est pas cosmétique : les lectures du groupe ont σ 10,2 ms (A15). Une
  bande morte de 5 ms n'a de sens que si l'estimateur est meilleur qu'elle : 32 lectures à 4 Hz
  ramènent σ à ≈ 2,3 ms. Avec les 5 lectures d'A15 (σ ≈ 5,6 ms) la boucle franchissait la bande
  au bruit et émettait 707 trims en 10 min ; avec 8 s elle en émet **21**.
- **Le slew** est ce qui fait qu'un trim est une *cadence*, pas un saut de position : la loi ne
  peut pas « rattraper » 40 ms en changeant brutalement de taux, elle y va à 2 ms/s au plus.

### 2.5 Ce que la loi regarde, et ce qu'elle refuse de croire

- **Rejet des lectures périmées** : une lecture à plus de 60 ms de la médiane de ses trois
  voisines est jetée (A15 : les aberrations mesurées sont à ±95…162 ms, le bruit légitime à ±35).
  Dans les runs, 5 à 23 lectures par session sont rejetées.
- **Hystérésis d'état** : une seule lecture non-`PLAYING` n'arrête rien ; il en faut **deux**
  consécutives (A36 : c'est ce bug qui coûtait 1,6 s de silence par lecture ratée). Testé
  explicitement (`test_one_stale_non_playing_read_is_ignored`).
- **Horloge** : l'offset récepteur↔contrôleur vient du ping/pong (`on_clock`), jamais d'une
  constante. Le §4 montre ce que coûte un offset faux.

## 3. Le signe de `DELTA_V2` (protocole §1)

Notations du protocole : `p_g`, `p_v` = têtes de lecture ; `L_g`, `L_v` = latences de rendu (délai
entre « la tête atteint m » et « le son/l'image sort ») ; le code maintient `p_g = p_v + delta`.

```
E_dev(m) = W_dev(m) + L_dev                      (instant de sortie du contenu m)

p_g = p_v + delta  ⇒  pour un même m :  W_g = W_v − delta

E_g = W_v − delta + L_g
E_v = W_v + L_v
O   = E_g − E_v = (L_g − L_v) − delta            (offset perçu)
```

`OBS_0` est mesuré avec `delta = 0`, donc `OBS_0 = L_g − L_v`. Pour annuler `O` :

> **`delta = +OBS_0`** (et non `−OBS_0`).

Contrôle par l'intuition, et c'est celui qui rattrape les erreurs : si la cuisine est entendue **en
retard** (`OBS_0 > 0`, mesuré +295 ms en v0.1, A28), sa tête de lecture doit être **en avance** sur
celle de la vidéo, donc `delta > 0`. Rien de paradoxal à ce que le récepteur soit ancré à
`m − DELTA_V2` quand le groupe est à `m` : c'est le *pipeline* du groupe qui est plus lent, pas son
horloge média.

En v0.2, la dérivation est identique avec **notre récepteur** à la place de la TV : `DELTA_V2 =
OBS_0` mesuré avec le récepteur qui joue, et le contrôle de linéarité du protocole §4.4 reste
obligatoire (régler, re-mesurer, l'offset doit *diminuer*).

Comment le signe est verrouillé dans le code :

1. `e = p_group − p_video − DELTA_V2` (fonction du contrat, jamais réécrite) ;
2. `e > 0` (la cuisine est en avance) ⇒ `u > 0` ⇒ `rate > 1` ⇒ le vidéo rattrape. Test :
   `TestErrorSign.test_positive_error_speeds_the_video_up` ;
3. `e < 0` ⇒ `rate < 1`. Test : `test_negative_error_slows_the_video_down` ;
4. l'invariant d'atterrissage : groupe à `m`, vidéo à `m − DELTA_V2`. Tests :
   `TestStartPlan.test_single_start_event`, `TestEvents.test_seek_moves_both_sides`.

Un signe inversé sur `DELTA_V2` ne ferait pas osciller la boucle : il la ferait converger vers un
offset faux de `2·DELTA_V2` (530 ms avec notre calibration), exactement le mode d'échec que
`notes/consensus.md` §5.3 décrit — aucune boucle ne détecte un `delta` faux.

## 4. Résultats de la simulation (`evidence/44-sim.json`)

Bruit injecté, toutes valeurs tracées :

| Paramètre | Valeur | Source |
|---|---|---|
| dérive du groupe | −12,4 ms / 10 min | `evidence/17` (600 s, 1148 points) |
| bruit de lecture du groupe | σ 10,2 ms + aberrations isolées ±95…162 ms | `evidence/17`, A15 |
| atterrissage PAUSE→SEEK→PLAY | moyenne −43,6 ms, σ 30 ms | `evidence/27` C, 8 essais |
| atterrissage LOAD | moyenne −314,7 ms, σ 202 ms | `evidence/27` A, 8 essais |
| RTT de lecture / horloge | 0…40 ms, estimateur = meilleur de 8 pings | `evidence/17` (RTT médian 20 ms), §3 |
| gain / blocage de cadence / dispersion de départ du récepteur | 1,0 / 0 ms / 0 ms | **non mesuré — tâche C** |

### 4.1 Les cas

`p95` = p95 des |médianes par fenêtre de 5 s| de l'erreur **physique** (`e_true`, sans bruit de
lecture) — c'est le critère du §2 du contrat. `lect.` = p95 d'une **lecture isolée** de l'erreur,
celle que le groupe bruite.

| cas | p95 (cible 10) | max | conv. (cible 10 s) | `\|u\|` émis / interne établi | trims | re-tombées | verdict |
|---|---|---|---|---|---|---|---|
| `cold_start` (10 min, 1 départ) | **2,5 ms** | 3,8 | **3,0 s** | 2,9e-4 / 3,9e-4 | 21 | 1 | PASS |
| `drift_only` (15 min, aucun événement) | **1,7 ms** | 2,4 | 3,0 s | 2,5e-4 / 3,9e-4 | 26 | 1 | PASS |
| `seek` (seek utilisateur à 120 s) | **5,2 ms** | 8,0 | 3,0 s | 2,5e-4 / 3,9e-4 | 15 | 1 | PASS |
| `pause_resume` (pause 20 s, reprise) | **5,4 ms** | 7,5 | 4,0 s | 2,5e-4 / 2,7e-4 | 10 | 1 | PASS |
| `stale_reads` (2 % d'états périmés) | **2,3 ms** | 3,3 | 4,0 s | 2,5e-4 / 3,4e-4 | 14 | 1 | PASS |
| `wrong_lead` (avance apprise fausse de 200 ms) | **3,5 ms** | 3,8 | 4,0 s | 2,5e-4 / 3,2e-4 | 14 | 1 | PASS |
| `clock_residual` (offset faux de 12 ms) | **12,7 ms** | 12,9 | 271 s | 2,4e-4 / 3,2e-4 | 17 | 1 | **FAIL attendu** |
| `rate_dead` (gain de cadence 0) | 8,3 ms | 8,9 | 1,0 s | 4,9e-4 / 5,6e-4 | 32 | 0 | PASS (marge 1,7 ms) |
| `pessimistic` (gain 0,5 + départ 40 ms) | **87,1 ms** | 93,5 | **206 s** | 2,0e-3 / 2,0e-3 | 19 | 2 | **FAIL attendu** |

Aucun de ces cas n'émet de `load`, de `seek` ni de `play_at` en régime établi : la colonne
« re-tombées » ne compte que celles des événements de transport (départ, seek, reprise), et les
tests `test_no_load_or_seek_or_relanding_once_steady` le vérifient cas par cas.

### 4.2 Ce que vaut chaque nombre

| Nombre | Ce qu'il prouve | Ce qu'il ne prouve pas |
|---|---|---|
| `p95 = 2,5 ms` (`cold_start`) | la loi tient le critère de 10 ms (et les 20 ms du contrat §2) sur 10 min, dérive comprise | que le groupe réel dérive de −12,4 ms/10 min ce soir-là (mesure unique, `evidence/17`) |
| `lect. p95 ≈ 21 ms, max ≈ 160 ms` | le bruit de lecture est bien celui d'A15 (σ 10,2, aberrations ±150) | rien sur la loi : aucune loi ne peut descendre sous le bruit de sa propre mesure — c'est pourquoi le critère du contrat porte sur la **médiane par fenêtre de 5 s**, et non sur le p95 brut |
| estimateur p95 ≈ 4,5–6 ms | la fenêtre de 8 s ramène bien σ 10,2 ms à ~2,3 ms, compatible avec une bande morte de 5 ms | que la vraie médiane des lectures soit gaussienne (les aberrations sont modélisées, pas expliquées) |
| `conv. = 3 s` | l'atterrissage (et sa re-tombée) fait la convergence, pas les trims | la latence réelle du récepteur à appliquer `play_at` (tâche C) |
| 21 trims en 570 s (~1 / 27 s), `\|u\| ≤ 4e-4` | les corrections restent à **≤ 0,7 cent** de hauteur (le plafond du contrat, `RATE_MAX` = 0,2 %, vaut ±3,5 cents : A5 chiffrait à 34 cents un clamp ±2 %, ce qui n'est pas ce réglage) | que le récepteur supporte un changement de taux sans blocage (tâche C : `rate_stall_ms`) |
| 0 `load` / 0 `seek` / 0 re-tombée après le régime établi | l'exigence d'acceptation « aucun trou après le départ » | la durée réelle d'un changement de taux chez le Chromecast |
| `drift_only \|u\|max = 2,5e-4` | le critère « jamais plus de `RATE_MAX/4` » est tenu à la moitié du plafond | que la dérive d'un vrai groupe ne fasse pas un saut (A30 : une marche de +28 ms au changement de vidéo n'est pas une dérive) |
| `clock_residual 12,7 ms` | **la boucle ne peut pas être meilleure que son horloge** : 12 ms d'erreur d'offset = 12 ms d'erreur vraie tenue sans broncher, alors que ce qu'elle *voit* est à 3,6 ms | comment réduire le résidu : c'est un choix de ping (meilleur de 8 → ≤ RTT_min/2, ici ~2 ms en nominal) |
| `rate_dead 8,3 ms` (PASS à 1,7 ms près) | sans actuateur de cadence, la dérive seule amène l'erreur à ~9 ms en 10 min : le critère tient **par chance**, pas par conception | une session plus longue, ou un quartz moins bon : la cadence n'est pas décorative |
| `pessimistic 87 ms, conv. 206 s` | quand le matériel ne suit pas, la loi le dit au lieu de le cacher | que le vrai gain soit 0,5 (personne ne l'a mesuré : tâche C) |

### 4.3 Pourquoi un départ en `LOAD` est interdit (mesuré, 40 tirages)

| politique de départ | premier atterrissage p50 | p95 | max | au-delà de 150 ms |
|---|---|---|---|---|
| PARQUE + `PLAY` (`evidence/27` C) | **20,2 ms** | 47,3 ms | 63,6 ms | **0 %** |
| `LOAD` (`evidence/27` A) | **281,4 ms** | 456,4 ms | 493,9 ms | **67,5 %** |

La re-tombée unique ramène les deux à p95 5,7 ms / 6,5 ms. Mais **avant** elle, un départ en
`LOAD` sort du budget perceptif de 150 ms dans deux cas sur trois : c'est exactement ce que
`evidence/27` disait (un LOAD est un actionneur grossier) et ce que le contrat §1 interdit en
imposant le parcage. Le simulateur montre le coût, pas seulement la règle.

### 4.4 Le cas pessimiste, en clair

Gain de cadence 0,5 (le récepteur ne fait que la moitié de ce qu'on demande) et dispersion de
départ 40 ms : après la re-tombée il reste ~40 ms que seule la cadence peut fermer, à 1 ms/s
effectif ⇒ **~200 s** pour repasser sous 10 ms, et une p95 de **87 ms** en attendant. Les critères
`steady_p95_le_10ms` et `convergence_le_10s` échouent, et `evidence/44-sim.json` porte le
`verdict` « FAIL (expected) » avec ces nombres. Si la tâche C mesure un gain de 0,5 et une
dispersion de 40 ms, **v0.2 ne tient pas son acceptation** et l'intégrateur doit le savoir avant
la porte matérielle — c'est précisément ce que le §1 du contrat demande de signaler.

## 5. Ce que la simulation ne prouve pas

1. **Rien sur le Chromecast.** Gain de `playbackRate`, coût d'un changement de cadence, dispersion
   réelle de `play_at` : trois inconnues (tâche C), trois paramètres (`--rate-gain`,
   `--rate-stall-ms`, `--start-dispersion-ms`). Les défauts (1,0 / 0 / 0) sont l'hypothèse
   optimiste ; le cas pessimiste borne l'autre côté.
2. **Rien sur le quartz du récepteur** : il est modélisé idéal (dérive 0). Une dérive réelle serait
   vue par la boucle comme une tendance de `e` et corrigée par la cadence ; son amplitude n'est pas
   connue.
3. **L'ordonnanceur de l'exécutant** : le simulateur déclenche le `PLAY` planifié à l'instant exact.
   La gigue d'une vraie boucle d'événements (10–50 ms) se retrouverait dans l'erreur d'atterrissage ;
   la σ 30 ms d'`evidence/27` a été mesurée à travers le vrai chemin, elle la contient déjà.
4. **La forme de la dispersion d'atterrissage** : 8 tirages seulement dans `evidence/27`, modélisés
   gaussiens. A25 note qu'en conditions réelles l'atterrissage est plus dispersé (~±80 ms) et
   **indépendant d'un essai à l'autre** — le simulateur permet de le régler
   (`--landing-sigma-ms 80`) mais les résultats du §4 sont à σ 30.
5. **L'offset perçu** : tout ce qui précède est l'erreur **numérique**. Les latences de rendu ne
   sont dans aucune boucle (consensus §5.4/§5.6) : la porte reste acoustique.
6. **Rien n'a été mesuré sur le LAN pour cette tâche**, et c'est volontaire (nuit silencieuse). Le
   simulateur est de la preuve sur *la loi*, pas sur *les appareils*.

## 6. Amendements proposés à `docs/v020-contracts.md` (non appliqués)

1. **§3.1 ne couvre que le récepteur.** Le §1 exige des commandes de transport sur le groupe
   (`play`, `pause`, `seek`, `stop`) mais `Command.kind` est documenté comme « a `MSG_*`
   constant », et aucun `MSG_*` ne désigne le groupe. La loi émet donc `group_play`,
   `group_pause`, `group_seek`, `group_stop` (docstring de `lib/v020/sync.py`), avec pour payload
   `{media, pause, at}`. Proposition : ajouter au contrat un §3.4 « contrôleur → groupe » avec ces
   quatre kinds et leurs payloads, ou un champ `target: video|group` sur `Command`.
2. **Un atterrissage doit pouvoir remettre la cadence à 1,0.** `playbackRate` survit à un `seek` et
   à un `play_at` : si un trim était actif, l'atterrissage suivant est calculé sur un appareil qui
   joue 0,04 % trop vite. La loi émet donc un `rate 1.0` **en tête** de l'événement d'atterrissage.
   Le tableau du §1 (« park/play/play_at » seulement) ne le prévoit pas : à ajouter.
3. **« convergence ≤ 10 s » n'est pas un critère de cadence.** Le §1 interdit les trims avant 30 s ;
   les 3–4 s mesurées viennent de l'atterrissage et de sa re-tombée. Proposition de libellé :
   « à ≤ 10 s d'un événement, |médiane 5 s de e| ≤ 10 ms grâce à l'atterrissage ; les trims
   maintiennent ensuite ».
4. **Le critère 10 ms se lit sur la médiane, pas sur le p95 brut.** Une lecture isolée du groupe a
   un p95 de ~21 ms et un max de ~160 ms (A15) : aucun assouplissement de la loi ne change cela.
   Le §2 le dit déjà pour la porte (médiane ±20 ms) ; préciser que le « p95 ≤ 10 ms » de la tâche F
   porte sur les médianes de fenêtre de 5 s (et sur `e_true` en simulation), sinon il sera mal lu.
5. **Signatures `start(now, video_ready, group_ready, lead_s)`.** Le contrat ne dit pas ce que sont
   `video_ready`/`group_ready`. La loi les traite comme des **positions médias** (secondes) des
   deux côtés garés et chargés — seule lecture cohérente avec une signature qui ne transporte
   aucune autre position. À documenter, sinon deux implémentations divergeront.

## 7. Reproduire

```sh
cd ~/opencode/nestaudiocast-wt/sync
./tests/v020/run.sh                                  # 57 tests hors ligne, ~1,7 s
./tests/v020/sim_v020.py --json evidence/44-sim.json # régénère la preuve (déterministe)
./tests/v020/sim_v020.py --rate-gain 0.5 --start-dispersion-ms 40   # tâche C, à la volée
./tools/prove.sh --fast                              # garde-fous v0.1 restés verts
```

Le fichier est déterministe à graine fixe (seul l'horodatage change) : deux exécutions donnent les
mêmes nombres, ce que `TestDeterminism` vérifie.
