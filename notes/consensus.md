# Consensus écrit — architecture retenue

Établi le 2026-10-25, après trois pistes menées en parallèle, une relecture croisée
indépendante (`notes/cross-review.md`) et la correction des affirmations qu'elle a réfutées.

> **Portée de ce document.** Il tranche **l'architecture**, et rien d'autre. Il n'affirme
> aucun chiffre d'offset perçu : à cette date, **aucune mesure A/V réelle n'existe**. Aucune
> décision irréversible, aucun achat.

---

## 1. La question posée

L'hypothèse principale était : *le conteneur se présente comme une cible de cast
supplémentaire dans l'app YouTube ; quand une vidéo y est castée, il récupère le flux, le
découpe et joue l'audio en cuisine et la vidéo sur la TV.*

Trois voies à départager pièces en main :
**A** l'appareil de cast virtuel · **B** le relais observateur de la session TV · **C** un pont
matériel ≤ 100 $ CA.

## 2. Verdicts, avec le niveau de preuve

| Piste | Verdict | Force de la preuve |
|---|---|---|
| **A — appareil de cast virtuel** | **Écartée** | **Faits établis** : un récepteur CASTv2 complet et une annonce mDNS fonctionnent, et un expéditeur Google réel du LAN le découvre seul. Sur 12 connexions consécutives il envoie le challenge d'authentification, reçoit notre réponse, **ferme proprement**, et n'envoie **jamais** `CONNECT` (`evidence/11`). **Non établi** : *pourquoi* (4 causes concurrentes, dont un trou de protocole de notre côté). Raison plus large et suffisante : même avec un certificat valide il faudrait le binaire du récepteur YouTube, non distribué. |
| **B — relais observateur** | **Retenue, en service** | **Faits établis** : la tête de lecture d'une session YouTube est lisible par CASTv2 (une lecture archivée, `evidence/05` ; une lecture non archivée avec contenu et position) ; un groupe accepte notre `LOAD` et ses 5 enceintes exécutent le récepteur (`evidence/06`) ; `SEEK` est opérant (±94 ms, `evidence/08`) et `SET_PLAYBACK_RATE` **ne l'est pas** (`evidence/17`) ; la boucle de contrôle est validée en simulation avec ses limites (`evidence/13`). Lecture continue désormais **démontrée** (sessions de 344 s, 413 s et 593 s suivies sans perte, `evidence/21`, `22`, `24`, `29`). **Offset perçu désormais mesuré** : **+295 ms** sans calibration, **−30 ms ± 3 ms** avec `delta = 0.295` (PASS, seuil 150 ms) — `evidence/audio/`, A28. **Non établi** : la série de 3 mesures sur ≥ 10 min sans dérive perçue. |
| **C — pont matériel ≤ 100 $** | **Sans objet** | Recherche seule, aucune mesure (aucun matériel). Trois blocages documentés : le Nest Audio n'a **aucune entrée audio** ; un groupe Cast n'accepte que de l'audio réseau (les entrées ligne/optique/ARC ne sont jamais propagées) ; retarder la vidéo coûte ≥ 312 $ US. Le seul montage qui marcherait (~110–130 $) **remplace** les Nest Audio — autre projet. Prix relevés le 2026-10-25, non revérifiés. |
| **D — tout dans un groupe Google (vidéo+audio sur un groupe TV+cuisine)** | **Réfutée** | **Mesuré 2026-10-25 10:40** (`evidence/18`, photo du propriétaire) : le groupe accepte un MP4 (`mediaCategory=VIDEO`) et la TV rejoint la session, mais n'affiche qu'une carte « Default Media Receiver · En Haut » sur l'écran ambiant, aucune image. Un groupe Cast est audio seulement. |

## 3. Recommandation

**Architecture B : relais observateur, alimentant un groupe Google Cast dédié à la cuisine.**

- La TV n'est **jamais** commandée : elle garde l'app YouTube native, son UI et son son. La
  perte d'UI acceptée d'avance ne se matérialise donc pas.
- Aucune cible de cast supplémentaire : le téléphone caste exactement comme aujourd'hui.
- Le CT 121 (`castrelay`, 192.168.2.21) lit la tête de lecture de la TV, récupère l'audio de la
  vidéo (`yt-dlp`) et pilote le groupe cuisine. **Un seul actuateur réel existe : `SEEK`**
  (±94 ms de dispersion, ~0.5 s de latence, trou audible).
  `SET_PLAYBACK_RATE` **n'a aucun effet sur ce matériel** : demandé 1.005 sur 60 s, ratio mesuré
  0.999957 ±0.00004 ; demandé 0.995, ratio 1.000008 (`evidence/17`). L'appareil rééchoit la
  valeur et ignore la commande. L'architecture ne repose donc plus dessus.
  Ce n'est pas grave, et c'est même plus simple : la dérive propre du groupe sur **10 minutes
  mesurées** est de **−12 ms ± 1 ms**. Une session de 10 min n'a pas besoin d'actuateur fin ;
  on aligne une fois, puis on ne corrige que si l'erreur dépasse 250 ms.
- `delta` est le **seul** paramètre de calibration. Ce n'est pas une constante physique qu'on
  calcule : c'est un **bouton réglé empiriquement** (mesure → réglage → re-mesure → contrôle de
  linéarité). **Signe : `delta = +OBS_0`**, et non l'inverse ; le protocole contient la
  dérivation et impose un contrôle de linéarité qui rattrape toute erreur de signe.
  Conséquence assumée : rien dans le système en marche ne détecte un `delta` devenu faux.

## 4. Décisions motivées

1. **On abandonne la réception du cast** — bloquée par une authentification cryptographique,
   constatée sur le LAN avec un expéditeur Google réel. Ce n'est pas un choix esthétique.
2. **On n'achète rien.** Aucune option ≤ 100 $ ne peut insérer un délai dans la voie des Nest
   Audio. Un achat serait une dépense sans effet sur le problème. Si un jour une tolérance
   élargie est acceptée, l'option 0 $ à essayer d'abord est le mode image du téléviseur
   (retard vidéo non gradué), **à mesurer avant tout achat**.
3. **On ne joue plus jamais sur un groupe existant.** Les trois groupes du réseau contiennent
   la TV (`evidence/09`, reproductible via `probe/group_membership.py`). Y démarrer une session
   **prend la TV**. Quatre prises de contrôle ont eu lieu le 2026-10-25 entre 00:35 et 00:52,
   et il est très probable qu'elles aient aussi **fait jouer de l'audio sur
   l'enceinte du téléviseur** (la TV était membre du groupe joué, à volume 0.4, jamais mute).
   Depuis : `lib/groupguard.py` refuse tout groupe non exclusivement composé d'enceintes,
   `lib/silence.py` arrête les sessions qu'il a créées, `lib/emergency.py` ne mute plus les
   groupes (muter un groupe change le volume de ses membres, donc de la TV).
4. **On mesure avec le même conteneur que la production (M4A)** et avec des fréquences **non
   harmoniquement liées** (1 kHz TV / 2310 Hz cuisine). Deux pièges mesurés : 70 ms d'écart de
   latence entre WAV et M4A, et un 2ᵉ harmonique du bip TV qui tombait pile dans la bande
   cuisine et fabriquait un faux offset nul.

## 5. Désaccords conservés (non tranchés par les preuves)

1. **Pourquoi la piste A échoue.** Quatre causes restent compatibles avec le silence observé :
   certificat non signé Google ; réponse malformée pour un parseur tiers ; **poignée de main
   incomplète de notre côté** (nous ne renvoyons jamais de challenge vers l'expéditeur) ; pair
   qui n'est pas un expéditeur complet. Le verdict « irréalisable » tient pour une raison plus
   large (binaire du récepteur YouTube non distribué), mais **la cause immédiate n'est pas
   prouvée**. Test décisif non fait : `chrome://media-router-internals` sur le portable.
2. **Fiabilité de l'observation de la TV.** C'est le socle de l'architecture retenue et il est
   le moins prouvé du dossier : 1 lecture réussie archivée, 1 timeout, 2 journaux vides. À
   établir par une observation continue de 10 min, journalisée.
3. **`delta` constant.** Rien ne garantit sa stabilité : mode image de la TV (+40 à +150 ms de
   latence vidéo selon les modèles), format du contenu (24p/30p, HDR/SDR), re-négociation du
   tampon du groupe, charge Wi-Fi (RTT mesurés jusqu'à 192 ms). La boucle ne peut **pas**
   détecter un `delta` faux : en simulation, un `delta` erroné de 200 ms est maintenu sans
   broncher (`evidence/13`). Seule la mesure acoustique protège.
4. **Le protocole mesure un écart audio-audio, pas image/son.** Il suppose que le bip de la TV
   est solidaire de son image. C'est **l'hypothèse fondatrice et elle n'est pas mesurée** ; la
   seule façon de la lever est une capture optique (240 fps), décrite nulle part en code.
5. **La dérive.** Les +55 ms/10 min d'une mesure de 20 s valent 0.48σ : indistinguables de
   zéro. Le critère « pas de dérive sur 10 min » exige une mesure de 10 min, qui n'existe pas
   encore côté groupe.
6. **`PLAYBACK_RATE` est tranché** (`evidence/17`) : c'est un écho, pas un levier. Reste ouvert
   ce qui en découle : sans actuateur fin, une **variation** de latence de rendu après
   calibration passe intégralement dans l'offset. En simulation, une latence de 0.50 s là où le
   relais en suppose 0.60 laisse un résidu constant de **+102 ms** (S4, `evidence/13`). C'est la
   limite dure de l'architecture, et elle ne se corrige que par une mesure acoustique répétée —
   ou par le micro permanent de la §7.
7. **Un seul point d'écoute pour cinq enceintes** : l'instrument prend la première arrivée,
   donc l'enceinte la plus proche du micro. Ce n'est pas l'offset perçu moyen de la pièce.

## 5 bis. Décisions et incidents du 2026-10-25 (soir)

- **Volume** : le groupe **suit le volume de la TV** (sur front, pour ne pas se battre avec un
  réglage fait sur le groupe). Vérifié : 4 changements en lecture = 4 miroirs exacts, **un seul
  rechargement**, erreur numérique −19 ms (`evidence/37`).
- **Tolérance d'atterrissage à 30 ms** (choix du propriétaire). Mesuré : c'est une **cible**, pas
  un plafond — l'actionneur gare le groupe en pause puis envoie PLAY, et atterrit avec une
  dispersion d'environ **±80 ms** par essai. À 30 ms : 2 rechargements par événement, médiane
  −5 ms, pire cas 94 ms. À 50 ms : 1,25 rechargement. Le budget perceptif (150 ms) est tenu dans
  les deux cas ; le choix est donc un compromis coupures/erreur, pas un enjeu de justesse.
- **Deux bugs qui rendaient la cuisine muette** en usage réel, tous deux corrigés et couverts par
  un test de régression (`tools/prove.sh` §5) :
  1. le silence mettait les enceintes à **niveau 0** au lieu de les muter ; or le volume maître
     d'un groupe **multiplie** le niveau de ses membres → groupe « en lecture » à 0,4 et silence
     total ;
  2. chaque **redémarrage du service** laissait les enceintes muettes (`ExecStopPost` mute, la
     nouvelle instance ne démute pas). Le relais **annule désormais son propre mute** au démarrage,
     membre par membre (uuid → IP via mDNS).
- **`delta` reste mesuré une fois** : aucune boucle ne détecte qu'il a changé. C'est la limite
  structurelle déjà consignée (§5.3).

## 6. Ce qui reste à faire avant de déclarer le PoC terminé

1. ~~Créer le groupe cuisine-only~~ — **fait** le 2026-10-25 (groupe « Cuisine », 5 Nest Audio,
   aucun appareil vidéo) ; le relais le résout par nom et la garde l'accepte.
2. ~~Prouver la lecture continue de la TV~~ — **fait** (sessions de 344 s, 413 s, 593 s).
3. ~~Mesurer l'offset perçu à 3 moments d'une session ≥ 10 min~~ — **fait** : 5 points sur une
   session continue de 911 s, offset **−20 / −20 / −20 / +8 / +34 ms**, et **dérive nulle
   (0.0 ± 0.0 ms/10 min)** à l'intérieur d'une vidéo ; une marche de ~+30 ms apparaît au
   **changement de vidéo** (pipeline différent), pas une dérive. `notes/00-labo.md` A30.
4. Transport : lecture, pause/reprise, seek, volumes, arrêt, fin de file, nouvelle session —
   **validés sur le matériel** ; session intacte de **690 s** : +7,7 ms de moyenne, σ 10,3 ms,
   dérive **−5,5 ± 1,2 ms/10 min** (`evidence/29-session-report.json`) ;
   l'**enchaînement automatique** est **suivi** (V1 → V2 sans intervention, cuisine recalée en
   ~25 s, `evidence/30-session-report.json`) ; la file injectée par le banc de test n'a pas pris
   deux fois sur cinq — c'est le **harnais**, pas le relais.
5. Refuser le PASS si la dispersion dépasse 40 ms ou si l'ambiguïté d'alias n'est pas levée.
6. ~~Rollback avec valeurs archivées~~ — **fait** (`evidence/28-rollback-test.json`, PASS : silence
   en 11,2 s, volumes identiques avant/après).
7. Faire lire le motif d'échec de la piste A (`chrome://media-router-internals`) si elle doit
   être close proprement.

## 7. Proposition retirée (2026-10-25) — micro permanent

**Retirée** : la dérive mesurée est de −12 ms/10 min (`evidence/17`), les appareils tiennent la synchro. Une calibration unique (enregistrement au téléphone) suffit pour le critère du PoC. Texte d'origine conservé ci-dessous pour mémoire.


Le point faible structurel de l'architecture B est que la boucle asservit des têtes de lecture
et ne voit aucune des deux latences de rendu : un `delta` faux est maintenu sans que rien ne
bronche. Une correction de conception le supprimerait :

> ajouter un **micro USB permanent** au CT 121, placé dans la pièce, qui mesure en continu
> l'écart entre le son de la TV (1 kHz) et celui de la cuisine (2310 Hz). `delta` cesse d'être
> une constante calibrée une fois : il devient une grandeur **mesurée et vérifiée en boucle**,
> et le relais peut corriger une dérive lente de latence de rendu.

Ce que ça change : le critère « ≤ 150 ms stable et sans dérive » devient **prouvable en
permanence**, pas seulement par trois mesures manuelles. Ce que ça coûte : un micro USB
(ordre de 25 $ CA, prix non vérifié) — donc, selon les règles du projet, **aucun achat sans
consensus et accord explicite du propriétaire**. Non décidé, non commandé.


---

## 8. v0.2.0 — l'expéditeur maison (décision du propriétaire, 2026-10-25 soir)

Le propriétaire a ouvert le cahier des charges de **v0.2.0** : ce n'est plus le relais qui suit une
session YouTube démarrée sur la TV, c'est **notre app qui possède la session**. Il a tranché les
points qui demandaient son accord, et il a fait l'action non automatisable.

### 8.1 Ce que le propriétaire a décidé et fait

| Décision | Valeur | Où c'est écrit |
|---|---|---|
| Compte développeur Cast | **acheté** (5 $ US, non remboursable) — l'achat exigé par la règle « aucun achat sans consensus » est donc autorisé et fait | ce paragraphe |
| Récepteur personnalisé | **VideoNetCaster**, `APP_ID = 28028CD2`, Custom Receiver, *Unpublished* | console Cast |
| URL du récepteur | **`http://192.168.2.21:7640/receiver.html`** — **gelée** : la changer coûte 15 min de propagation | idem |
| Appareil enregistré | **Chromecast Salon** `192.168.2.80`, numéro de série saisi par le propriétaire → il exécute les apps normales **et** la nôtre, une à la fois | idem |
| Achat matériel | **aucun** : on enregistre le Chromecast déjà sur la TV (« no purchase, no spare ») | cahier des charges v0.2.0 |
| GPU | une RTX 3070 Ti **peut** être passée au CT 121 (partagée avec Jellyfin et Ollama : l'ajouter est permis, les déranger ne l'est pas) | idem |
| Live | hors portée | idem |
| Heures calmes | **non voulues** (propriétaire, 2026-10-26) : `QUIET_START`/`QUIET_END` restent vides = désactivé ; retirées du gate matériel. Le code reste, inactif | cette ligne |

### 8.2 Le renversement d'architecture, et pourquoi il est forcé

v0.1 suivait la TV parce qu'on ne pouvait pas posséder la session. v0.2 la possède : les deux
sessions sont les nôtres, donc **le départ se choisit** au lieu de se subir. Conséquence
structurelle, imposée par les mesures déjà au dossier :

- `SET_PLAYBACK_RATE` est un **no-op sur le groupe** (demandé 1.005, mesuré 0.999957, `evidence/17`) ;
- le cahier des charges v0.2.0 **interdit** tout rechargement après le départ (« corrections = rate
  trims, never reloads »).

Il ne reste donc **qu'un seul actionneur fin possible : `playbackRate` sur notre récepteur**. Le
groupe devient la **référence libre** (dérive mesurée −12,4 ± 1,0 ms/10 min) et la vidéo suit. C'est
le pari central de v0.2.0, et il est **non vérifié à ce jour** : tâche C, à mesurer avant toute
autre conclusion. Si le Chromecast ignore `playbackRate` comme le groupe, le critère ne peut pas
être tenu et le propriétaire doit le savoir avant qu'un contournement ne soit écrit.

### 8.3 Critère resserré (v0.2.0, remplace le 150 ms du PoC)

| Grandeur | Seuil | Ce que ça change |
|---|---|---|
| erreur **numérique** en régime | **≤ 20 ms** (médiane par fenêtre de 5 s) | dix fois plus serré que les 30-70 ms de v0.1 ; c'est ce que la boucle asservit |
| offset **perçu** | **≤ 15 ms** à **un** point calibré, 3 points acoustiques sur ≥ 10 min | l'instrument a un biais connu de **+6 ms** (A9/A28) : la marge réelle est mince, il faut le dire |
| premier frame | **≤ 15 s** après le partage | téléchargement interdit sur disque → proxy de plages + MPD DASH |
| coupures après le départ | **aucune** | d'où l'interdiction de recharger |

Conséquences assumées, à ne pas redécouvrir plus tard : le coût de ces seuils est que **toute**
erreur de départ doit être absorbée par une variation de cadence, donc par une variation de hauteur
(±0,2 % = ±3,5 cents, inaudible) — et que la latence de rendu du Chromecast devient une constante
qu'aucune boucle ne voit (consensus §5.6, toujours vrai).

### 8.4 Ce qui reste non prouvé, et qui décide de la livraison

1. `playbackRate` est-il honoré sur le Chromecast ? (tâche C, la mesure décisive)
2. Le codec : `avc1` ≤ 1080p suffit-il, ou faut-il transcoder (donc le GPU) ? (tâche C puis B)
3. La dispersion de départ côté vidéo (`play_at`) — inconnue, mesurée en C.
4. L'offset perçu à 15 ms : mesurable, mais l'instrument est à ±3-6 ms et biaisé +6 ms.
5. Le comportement quand un autre expéditeur prend le Chromecast : exigé, non mesuré.

Rien de tout cela n'est affirmé. Chaque tâche doit rapporter « non vérifié, parce que… » plutôt
qu'un chiffre inventé.

### 8.5 Désaccords ouverts après la 2ᵉ relecture (2026-10-26)

Relecture faite sur les fichiers, pas sur les rapports. Rien ici n'est tranché : chaque point dit
ce qui le trancherait.

1. **L'erreur d'horloge récepteur ↔ groupe est-elle absorbée par la calibration ?** L'amendement
   §10.10 des contrats l'affirmait : un biais **constant** est absorbé par `DELTA_V2`. C'est vrai
   pour un biais constant d'une session à l'autre ; mais l'estimateur (tâche D) **ré-estime**
   l'horloge à chaque session, et la part aléatoire de son erreur change donc à chaque fois. Le cas
   de simulation `clock_residual` (12 ms → p95 12,7 ms, convergence 271 s) n'est pas « un scénario
   de re-calibration » : c'est la borne si l'erreur par session atteint 12 ms. Le fake donne
   0,5 ms ; **le vrai chiffre se mesure en C (d)** sur l'appareil. Tant qu'il n'existe pas, §10.10
   est une hypothèse.
2. **Le départ côté groupe : 44 ms (banc) ou ±80 ms (usage réel) ?** §10.3 prend la moyenne du
   banc muet (`evidence/27`, σ 30 ms). En usage réel v0.1 a mesuré ~±80 ms (A25). Simulé depuis
   (`evidence/44b-sim-video-side.json`, `group_landing_80`) : **PASS**, p95 1,4 ms — la vidéo
   se cale sur la position mesurée du groupe, donc la dispersion du groupe est absorbée. Reste non
   prouvé sur le matériel.
3. **Le bruit de lecture de la tête vidéo n'est pas filtré.** Tous les cas qui passaient
   supposaient un côté vidéo parfait. Nouveaux cas (même fichier) : dispersion de `play_at` 40 et
   80 ms → **PASS** (3,0 et 4,1 ms) ; **bruit de lecture 10 ms → FAIL sur la convergence (36 s
   pour 10 s exigées)**, l'erreur en régime tenant pourtant (p95 3,0 ms) : la loi poursuit le
   bruit (39 ajustements de cadence). Combiné réaliste (40 + 10 + 80 + un seek) : FAIL, convergence
   44 s, aucun rechargement. Correction probable : filtrer la tête vidéo comme celle du groupe
   (médiane), **à décider quand C aura mesuré le vrai bruit** — le lecteur de la plateforme montre
   jusqu'à 148-168 ms d'écart (`evidence/41b`, `residual_max_ms`), notre page lit `currentTime`
   directement et devrait faire mieux : non mesuré.
4. **La garde réseau des tests ne couvre pas les sous-processus** (`docs/dev.md`). Un test qui lance
   un script (`test_group.py` → `tools/group_probe_v020.py --run`) compte sur les refus du script
   lui-même. Ces refus sont testés ; ils ne sont pas imposés par le lanceur.
5. ~~**Nommage des heures calmes.**~~ Sans objet : le propriétaire ne veut pas d'heures calmes
   (2026-10-26). Si un jour on les active : `QUIET_START..QUIET_END` est, selon §7, la fenêtre où
   le son **peut** démarrer — l'inverse de ce que le nom suggère ; renommer avant d'y mettre une
   valeur.


### 8.6 Blocage du 2026-10-26 : l'app n'est pas connue de l'appareil

Mesure (lecture seule, `GET_APP_AVAILABILITY`) après le redémarrage demandé par le propriétaire :

| appareil | `28028CD2` | `CC1AD845` |
|---|---|---|
| Chromecast Salon `.80` (uptime 2061 s après redémarrage) | **APP_UNAVAILABLE** | APP_AVAILABLE |
| Chromecast Sous-Sol `.81`, Basement `.128`, Salon Orange `.66`, Salon Jaune `.67`, Chambre `.120`, TV chambre `.130` | APP_UNAVAILABLE | APP_AVAILABLE |

`LAUNCH 28028CD2` → `LAUNCH_ERROR / NOT_FOUND`. Aucun appareil du LAN ne connaît donc l'APP_ID, y
compris celui que le propriétaire a enregistré. La pile Cast est saine (les autres apps répondent) :
c'est l'enregistrement de l'appareil dans la console qui n'a pas pris effet. Trois causes restent,
non départageables d'ici : numéro de série saisi qui ne correspond pas à cet appareil, enregistrement
fait sous un autre compte Google que l'app, ou propagation non terminée. **À vérifier côté console
par le propriétaire** (onglet *Devices*, numéro de série comparé à celui de l'appareil).
Conséquence : tout ce qui demande **notre page** reste non mesuré (horloge, dispersion de départ,
bruit de `currentTime` côté page, gain de cadence via la page). Ce qui ne la demande pas a été mesuré
(lecteur de la plateforme, `evidence/49`).
