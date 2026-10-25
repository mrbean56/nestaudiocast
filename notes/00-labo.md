# Cahier de laboratoire — relais Cast cuisine/TV

Règle du projet : chaque tentative = **hypothèse → mesure → conclusion → étape suivante**.
Aucune affirmation sans preuve exécutée sur le LAN. Preuves dans `evidence/`.

> **Statut au 2026-10-25 02:00** : aucune mesure d'offset **perçu** n'a encore été faite. Le
> trajet complet `TV → relais → groupe → oreille` n'a jamais tourné, faute de groupe
> cuisine-only. Tout ce qui est plus bas valide des **briques**, pas le résultat.
>
> Les entrées marquées **[corrigé]** ont été redressées après la relecture croisée
> (`notes/cross-review.md`), qui a trouvé plusieurs affirmations surévaluées et une erreur de
> signe. Les corrections sont conservées visiblement, pas effacées.

Environnement : LXC 118 `opencode` (192.168.2.18) puis CT 121 `castrelay` (192.168.2.21),
Proxmox pve 192.168.2.3. Client CASTv2 maison mono-thread (`lib/castv2.py`).

---

## A1 — Inventaire réel des cibles Cast (00:33) **[corrigé]**

**Hypothèse** : les cibles annoncées existent et sont joignables depuis le LAN.

**Mesure** (`evidence/01-discover-cast.txt`) : mDNS `_googlecast._tcp` → **16 cibles**
(la note disait 15 : erreur de comptage, corrigée). Cuisine = **5 Nest Audio** (.61–.65).
TV = **« Chromecast Salon » .80**, modèle annoncé `Chromecast`, `caps=201221`.
Autres : Ultra .81, Basement TV .128, Chromecast HD .130, 4 Nest Hub (.66, .67, .119, .120).
3 groupes : « Groupe Tous », « En Haut », « Dehors ».

**Correction** : la « 3e génération » de la TV n'est établie par aucune preuve — `md` vaut
`Chromecast` pour deux appareils différents. Ne pas l'affirmer ; seule la capacité compte.

## A2 — Peut-on observer la session YouTube de la TV ? **[corrigé, partiellement prouvé]**

**Hypothèse** : une session YouTube expose son état via CASTv2, ce qui donne une horloge de
référence sans toucher à la lecture.

**Mesure — ce que les fichiers contiennent réellement** :
- `evidence/04-raw-castv2-probe.json` : **échec** (`media_error: timeout … GET_STATUS`).
  La note citait initialement ce fichier pour des valeurs (contenu, tête de lecture) qu'il ne
  contient pas. C'était un mésusage de source.
- `evidence/05-device-snapshot.log` : **une** lecture réussie —
  `{"app": "YouTube", "state": "PLAYING", "t": 1675.659, "content": "VyNgbQKORiA"}`.
- Une lecture unique, non archivée, avait également produit
  `contentId="VyNgbQKORiA", currentTime=1595.501, mediaSessionId=2147215415` : ces valeurs
  n'existent nulle part dans `evidence/` et ne doivent pas être citées.
- Les deux essais du relais réel (`evidence/relay-20261025-011350.jsonl`,
  `…012111.jsonl`) font **0 octet** : le relais n'a rien journalisé (aucun battement de cœur
  quand la TV ne joue pas — corrigé depuis).

**Conclusion honnête** : le chemin est **plausible et a fonctionné au moins une fois**, mais
il n'est **pas démontré fiable** : 1 succès archivé, 1 timeout documenté, 2 journaux muets.
Il faut une observation continue de 10 min pour le prouver. C'était l'affirmation la plus
surévaluée du dossier, et c'est celle sur laquelle repose toute l'architecture retenue.
**Suivant** : observation continue réelle, journalisée, dès qu'une session existe.

## A3 — Un groupe Cast accepte-t-il notre propre flux ? (00:35) **[corrigé]**

**Hypothèse** : un groupe n'accepte que les apps musicales et refusera un `LOAD` manuel.

**Mesure** (`evidence/06-group-load-test.json`) : `LAUNCH` Default Media Receiver sur
« Groupe Tous » → OK en 1362 ms ; `LOAD` de `sync_click.wav` → `PLAYING`, tête de lecture
0 → 14.988 s ; les 5 Nest Audio exécutent le récepteur du groupe.

**Conclusion** : au **niveau protocole**, notre flux est accepté et la tête de lecture suit.
**Correction importante** : aucun test n'a jamais confirmé qu'un **son** sort d'une enceinte —
tout était muet par construction. Et ce test a très probablement fait jouer de l'audio sur la
**TV** (elle est membre du groupe, à `level=0.4`, jamais mute) : voir A6.
**Suivant** : précision de positionnement.

## A4 — Latence de démarrage et cadence (00:35 → 00:40) **[corrigé]**

**Mesure** (`evidence/06`, `07-seek-precision.json`, `16-single-device-latency.json`) :
- La tête de lecture, après démarrage, suit l'horloge murale avec un décalage constant de
  **−0.381 s médian, σ 7.9 ms** (recalculé). Mais l'origine de cette constante est le
  **premier échantillon**, situé 268 ms après le `LOAD` : le début réel du flux est donc à
  **~0.65 s après le `LOAD`**, pas 0.37 s.
- Estimations indépendantes du démarrage : **0.54–0.65 s** (`06`, `07`, `16`). La note
  initiale disait 0.37–0.40 s : **faux**, et le simulateur utilisait 0.40 s.
- Les deux premiers échantillons de `07` sont aberrants (l'appareil annonce `PLAYING` avant
  de bufferiser, puis repasse `BUFFERING`) : un état qui recule. Ne pas dater le son avec
  `PLAYING`.
- Cadence sur 20 s : pente **1.000091** → **+54.5 ms/10 min**, ce qui est **exact** — mais
  l'erreur-type sur la pente est **±114 ms/10 min (1σ)**. Les +55 ms/10 min valent 0.48σ :
  ils sont **indistinguables de zéro** à cette durée.

**Conclusion** : démarrage ~0.55 s (à re-mesurer sur le groupe), dérive **non déterminée** à
20 s. Une mesure de 10 min est nécessaire et suffisante : l'erreur-type décroît en T^(−3/2).

## A5 — SEEK et contrôle de cadence (00:47) **[corrigé]**

**Mesure** (`evidence/08-groups-seek-rate.json`) :
- 8 `SEEK` sur le groupe, référence = instant de la commande : erreur **−410 à −597 ms**
  (latence de prise d'effet ~0.51 s, σ 74 ms), dispersion **±93.8 ms** (recalculée).
- `SET_PLAYBACK_RATE` **accepté** (1.005 / 0.995 / 1.0). Le gain était mesuré sur **deux
  points espacés de 4.4 s** → incertitude ±0.23 %, soit inutilisable.
- Sur un appareil seul, `SEEK` est bien plus précis : ±26 ms (WAV) / ±47 ms (M4A), et il n'a
  pas la même latence de prise d'effet → `SEEK_LEAD_S = 0.5` est **propre au groupe**.

**Correction** : la note affirmait que le groupe annonce `PLAYBACK_RATE` dans
`supportedMediaCommands=274447`. C'est **faux** : ce masque n'est pas décodable sans le proto
de référence, et le bit attendu (2048 ou 4096 selon la table) ne tombe pas juste. Ce qui est
prouvé, c'est que la **commande est acceptée et obéie** — pas qu'elle est annoncée.
Nouvelle mesure en cours sur 60 s (`evidence/17-…`, `probe/rate_gain.py`) pour donner un gain
avec une incertitude.
**Correction aussi** : « fin, continu, **inaudible** » est faux. La boucle clampe à ±2 %, soit
**±34 cents** de décalage de hauteur : c'est audible sur de la musique tenue. C'est acceptable
pour un PoC, pas « inaudible ».

## A6 — INCIDENT : tous les groupes contiennent la TV (00:52) **[corrigé]**

**Mesure** (`evidence/09-group-membership-multizone.json`, `10-groupguard-report.log`) via
`MULTIZONE_STATUS`, en lecture seule :

| Groupe | Membres | Sûr ? |
|---|---|---|
| Groupe Tous | 5 Nest Audio cuisine + **Chromecast Sous-Sol** + **Chromecast Salon (TV)** | non |
| En Haut | 5 Nest Audio cuisine + **Chromecast Salon (TV)** | non |
| Dehors | **Chromecast Sous-Sol** + **Chromecast Salon (TV)** | non |

**Portée réelle de l'incident — pire que ce que la première note disait** :
`probe/groups_seek_rate.py` démarre une session sur **les trois groupes** ; avec `06` et `07`,
cela fait **au moins quatre prises de contrôle de la TV**, pas deux. Et comme la TV n'a jamais
été mute (`silence(touch_tv=False)`, jamais utilisé) alors qu'elle était membre du groupe joué
à `level=0.4`, **il est très probable que ces tests aient fait jouer de l'audio sur
l'enceinte du téléviseur**, en plus d'interrompre la lecture en cours.

**Leçon opérationnelle** : `MULTIZONE_STATUS` donne la liste des membres **sans jouer**. Tout
groupe contenant autre chose qu'une enceinte audio (`caps == 196612`) est interdit de lecture
— c'est `lib/groupguard.py`. Et `lib/emergency.py` ne touche plus aux groupes (muter un groupe
propage le volume à ses membres, donc à la TV).

## A7 — Piste 1 : récepteur Cast virtuel (00:56 → 01:15) **[corrigé]**

Voir `notes/track1-protocol.md`, `evidence/11`, `evidence/12`.
Correction du raisonnement : la première version concluait « l'expéditeur abandonne donc c'est
le certificat », en s'appuyant sur `sender_disconnected = 0`. Or le récepteur **ne journalise
pas** une fermeture propre (`if not data: break` court-circuite le `except`). Ce compteur ne
distingue donc rien. Ce qui reste solide : l'expéditeur n'envoie **que** le challenge
d'authentification, **jamais** `CONNECT`, et cela **12 fois depuis .2 et 14 fois depuis .115**.
Le verdict (« irréalisable sans certificat signé Google ») reste probable, mais il n'est pas
prouvé par ce seul silence — voir `notes/track1-protocol.md` §3 pour les 4 causes concurrentes.

## A8 — Boucle de contrôle en simulation (01:25) **[corrigé]**

**Mesure** (`evidence/13-controller-sim.json`) :

| Scénario | Dérive | Erreur max (hors événements) | SEEK | changements de cadence |
|---|---|---|---|---|
| S1 dérive « réelle » + seek/pause/changement de contenu/arrêt/nouvelle session | 91 ppm | **44.5 ms** | 2 | 15 |
| S2 dérive 55× pire | 3000 ppm | **47.4 ms** | 0 | 120 |
| S3 `delta` = 200 ms | 91 ppm | 22.6 ms | 0 | 0 |

**Corrections** :
- « tient sous 50 ms » est vrai **après filtrage** : sans exclure les échantillons de seek,
  S1 contient une erreur de −585 s (le seek utilisateur vers 600 s) et le taux « dans ±50 ms »
  tombe à 99.2 %. Le filtre est explicite dans `sim.analyze()`, il faut le dire.
- **S1 ne teste pas la dérive** : 91 ppm × 72 s = 6.6 ms, sous le seuil mort de 30 ms. Seul S2
  exerce réellement une dérive, et à une valeur 55× irréaliste.
- **S3 ne prouve pas que `delta` est suivi à ±2 ms** (max 22.6 ms) et surtout il prouve le
  contraire de ce qui était écrit : la boucle maintient **sans broncher** un `delta` faux de
  200 ms. Aucune boucle ne peut valider `delta` ; seule la mesure acoustique le fait.
- Le simulateur utilisait un démarrage de 0.40 s alors que la mesure donne 0.55–0.65 s
  (corrigé depuis : `START_LATENCY = 0.55`).

## A9 — Validation de l'instrument de mesure (01:32) **[corrigé]**

La première version ne testait que |offset| ≤ 120 ms avec un lit à 90 Hz (hors bandes) et des
sinusoïdes pures. Elle passait pendant que l'analyseur rendait `−100 ms` pour un vrai
`+900 ms`, avec verdict PASS.

**Mesure après correction** (`evidence/14-analyzer-selftest.json`, 10 cas) :
- offsets 0 / +37 / +120 / −85 ms → mesurés à **±1 ms** ;
- +400 ms → mesuré juste, verdict FAIL (correct) ;
- **+600 et +900 ms** → repliés par la période de 1 s, mais la **famille d'alias est imprimée**
  et contient la vérité ; `--expect-ms` lève l'ambiguïté ;
- **distorsion** (bip TV avec 2ᵉ harmonique) → non piégé, grâce à un clic cuisine à **2310 Hz**
  (non harmonique de 1 kHz) ;
- **lit musical avec énergie dans les deux bandes** → mesure encore (62 ms pour 60 ms) ;
- dérive vraie 8 ms/min → **8.0 ms/min, σ 0.3**.

**Conclusion** : l'instrument est valide **dans son domaine**, et il refuse de conclure quand
il ne peut pas (alias, dispersion > 40 ms, moins de 8 paires, soupçon d'harmonique).
**Ce qu'il ne fait toujours pas** : voir l'image. Il mesure un écart audio-audio, pas
l'offset image/son (§6 du protocole).

## A10 — Arrêt d'urgence (01:17) **[corrigé]**

**Mesure** (`evidence/15-panic-test.log`, daté 01:17:03 — la note disait à tort 01:38) :
`lib/emergency.py` arrête le récepteur virtuel, mute les 5 Nest Audio, vérifie qu'aucune
session ne tourne.
**Correction** : ce journal ne contient **aucune valeur de volume**, donc il ne peut pas
« confirmer la restauration valeur par valeur » comme l'écrivait la note. La restauration a
été vérifiée à la main ensuite (volumes remis à 0.0833/0.55/0.0833 et groupes à
0.2619/0.2917/0.2417), mais **sans artefact dédié**. À refaire proprement avec un avant/après.

## A11 — Conteneur dédié CT 121 (01:19)

CT 121 `castrelay` créé sur pve : Debian 13, 2 vCPU, 1 GiB, 6 GiB local-lvm, non privilégié,
`onboot=1`, IP **192.168.2.21** (hors plage DHCP .100–.150), DNS AdGuard .11. Vérifié :
16 cibles Cast vues, `groupguard` opérationnel, `selftest_analyzer` PASS, `--dry-run` lit la
TV, `local_ip` = 192.168.2.21, `run-relay.sh` refuse de démarrer sans `GROUP` (exit 78),
service installé **désactivé**, ajouté à la sauvegarde 03:45 (VMID 121), `/README.md` écrit.

## A12 — Mode mesure exécutable (01:22)

`--measure-tone 2000` (puis 2310 après relecture) joue une piste de clics sur la même tête de
lecture que la vidéo. Pistes générées : `assets/click_train_2k.{wav,m4a}` (720 s).

## A13 — La latence dépend du conteneur (01:26)

**Mesure** (`evidence/16-single-device-latency.json`), sur **un** Nest Audio, muté :

| Format | délai jusqu'à `PLAYING` | erreurs SEEK | dispersion |
|---|---|---|---|
| WAV | **0.542 s** | +67.9, +41.9, +70.8, +14.9 ms | 26 ms |
| M4A/AAC | **0.612 s** | −27.3, −19.8, −28.1, +68.7 ms | 47 ms |
| *groupe* (A4/A5) | ~0.65 s | −410 à −597 ms | 94 ms |

**Conclusion** : 70 ms d'écart entre WAV et M4A → la mesure doit se faire **dans le même
conteneur que la production**. Réserve honnête : **n=1 par format**, une seule enceinte, et
les deux essais ne partent pas du même état (`IDLE` vs `PLAYING`) — la décision est bonne, la
preuve est mince.

## A15 — `PLAYBACK_RATE` est un écho, et la dérive réelle est faible (01:48)

**Hypothèse** (héritée de A5) : `SET_PLAYBACK_RATE` est le levier fin qui permet de tenir
10 min sans dérive.

**Mesure** (`evidence/17-rate-gain-and-drift.json`, `probe/rate_gain.py`) — moindres carrés sur
des séries longues, un seul Nest Audio, muté :

| Demande | Ratio média/mur mesuré | Gain réel | Durée |
|---|---|---|---|
| 1.005 | **0.999957** ± 0.00004 | **−0.0043 %** ± 0.004 % | 60 s, 116 points |
| 0.995 | **1.000008** ± 0.00004 | **+0.0008 %** ± 0.004 % | 60 s, 116 points |
| 1.000 | 0.999979 ± 0.000002 | −0.0021 % | **600 s, 1148 points** |

**Conclusion** : l'appareil **accepte** la commande, la **réécho** dans son statut
(`playbackRate: 1.005`) et **ne change pas la cadence**. L'actuateur fin n'existe pas. L'ancien
« gain honoré » de A5 était un artefact d'une mesure à deux points sur 4.4 s.

En revanche, la dérive sur **10 minutes réelles** est de **−12.4 ms ± 1.0 ms**, soit très
en-deçà du budget de 150 ms. Le résidu de la tête de lecture par rapport à un ajustement
linéaire est de **σ 10.2 ms**, avec quelques **échantillons isolés aberrants** (+162/−156 ms,
+114/−95 ms) qui reviennent au point suivant : des lectures périmées, pas des sauts réels.

**Conséquences appliquées au code** :
- `--use-rate` est **désactivé par défaut** (documenté comme no-op sur ce matériel) ;
- l'erreur est **filtrée** (médiane sur 5 échantillons + rejet d'un échantillon qui contredit
  ses voisins) pour qu'une lecture périmée ne déclenche pas un SEEK inutile ;
- un **réalignement unique** après démarrage, et plus aucune correction tant que |erreur|
  reste sous 250 ms — ce qui est le cas sur 10 min avec −12 ms de dérive.

## A14 — Relecture croisée : ce qu'elle a cassé (01:45)

Relecture indépendante (`notes/cross-review.md`, 389 lignes) sur l'ensemble des preuves. Elle a
re-dérivé les chiffres et trouvé, en plus des corrections ci-dessus :

- **deux affirmations sans preuve** dans le dossier : la MAC et le service `llama.cpp` de
  `.2`, et la génération de la TV ;
- **`evidence/08.members` est structurellement faux** (`transport == tr` ne peut jamais être
  vrai pour un membre) : ce fichier ne prouve pas l'appartenance — `09` le fait ;
- **`evidence/09` n'avait pas de générateur dans le dépôt** → ajouté (`probe/group_membership.py`) ;
- **`evidence/02` et `03` sont des runs cassés** (exceptions) : conservés mais annotés dans
  `evidence/README.md`, à ne pas citer ;
- **le mute des groupes dans `emergency.py` pouvait changer le volume de la TV** → corrigé ;
- le protocole acoustique ne voit pas l'image (hypothèse « bip ≡ flash » non mesurée).

**Ce que la relecture confirme comme solide** : `06` (le groupe accepte notre flux), `09`
(appartenance), `13` et `14` (reproductibles à ≤0.1 ms). C'est peu, et c'est dit.

## A18 — Un groupe Cast peut-il afficher la vidéo sur son Chromecast ? (2026-10-25 10:40, autorisé par le propriétaire)
- **Hypothèse** : si oui, envoyer vidéo+audio à un groupe TV+cuisine laisse la synchro multizone de Google (et son curseur « Group delay correction ») faire tout le travail.
- **Mesure** : MP4 H.264 720p (mire `testsrc`, audio AAC muet) chargé sur « En Haut » (5 Nest Audio + Chromecast Salon), 90 s. `evidence/18-group-video-test.json` : groupe `mediaCategory=VIDEO`, piste VIDEO listée, PLAYING à +4 s, tête de lecture régulière ; la TV quitte Backdrop et exécute le récepteur du groupe (705D30C6). Photo du propriétaire à 10:41 (`evidence/18-tv-photo-1041.jpg`) : écran ambiant (montgolfière) + carte « Default Media Receiver · En Haut » avec icône note de musique. **Aucune image vidéo.**
- **Conclusion** : un groupe Cast est audio seulement ; le membre Chromecast joue le son et affiche une carte « en lecture ». La voie « tout dans un groupe Google » est **réfutée**. Retour à B (relais observateur + calibration unique de delta). Le curseur Google Home reste utilisable comme réglage fin côté cuisine (à vérifier : s'applique-t-il aux sessions DMR ?).
- Note : dans le journal, le champ `t` des lignes `group_media` est la tête de lecture (collision de clé), pas l'horodatage.

## A19 — Groupe « Cuisine » et pilotage autonome de la TV (2026-10-25 10:47–11:00)
- Groupe « Cuisine » créé par le propriétaire : `192.168.2.61:32221`, 5 membres, tous `caps=196612` → la garde l'accepte (premier groupe sûr). Le relais le résout désormais **par nom** via mDNS (le port est dynamique).
- `tools/yt_tv.py` lance l'app YouTube officielle sur la TV et y joue/enfile des vidéos (même récepteur 233637DE que le téléphone, via le protocole lounge de YouTube). Refuse d'agir si la TV exécute autre chose que Backdrop/YouTube. **Erreur corrigée** : sa commande `status` passait par le contrôleur média de pychromecast, qui **lance** le DMR → la TV (alors sur Backdrop, inoccupée) est passée sur DMR ~1 min, arrêtée aussitôt ; `status` est maintenant en lecture seule (CASTv2 brut).
- Vidéos de test : `QzomK1fdSUg` (600 s, bip 350 Hz chaque seconde, période 1.0004 s) et `K0uxjdcZWyk` (600 s, bip 1714 Hz). `tools/measure_track.py` produit pour chacune une piste cuisine à 2310 Hz aux instants exacts des bips (contrôle après encodage AAC : ≤ 5,7 ms).

## A20 — La tête de lecture YouTube de la TV est en dents de scie (11:00)
- **Mesure** (`evidence/19-tv-playhead-dryrun.jsonl`, 198 lectures / 60 s, RTT médian 20 ms) : résidu σ 55 ms, −135…+86 ms. Motif : entre deux rafraîchissements la valeur avance à ~0,85×, puis saute de ~+150 ms toutes les 1–1,5 s. Le bruit ne vient pas du réseau (corrélation résidu/RTT −0,07).
- **Conclusion** : une lecture isolée est fausse jusqu'à ±140 ms → inutilisable telle quelle pour une cible de 150 ms. L'**enveloppe haute** (2ᵉ plus grand `t − wall` sur 10 s) est stable à σ 4,6 ms (p2p 12 ms) → `TvClock` dans le relais. Réinitialisée sur pause, changement de vidéo, saut > 0,4 s (seek).

## A21 — Premier relais réel TV → « Cuisine » (11:00, `evidence/20-relay-first-real.jsonl`)
- Erreur numérique (tête groupe − tête TV filtrée) **+224 ms ± 9 ms** pendant 100 s, dérive +1 ms/min ; fin de vidéo → TV `IDLE` → cuisine arrêtée.
- Le +224 ms = biais de démarrage (le groupe commence ~0,38 s après LOAD, pas 0,6). → **apprentissage du biais** : après chaque démarrage, l'erreur d'atterrissage est mesurée et corrigée par un nouveau LOAD (≤ 2 fois), la correction est conservée pour les démarrages suivants.
- **Bug trouvé en réel** (`evidence/21-relay-scenario-run1.jsonl`) : juste après LOAD le groupe rapporte la position demandée avant de jouer → +0,6 s lu comme erreur → rechargement toutes les 0,5 s pendant 30 s (cuisine hachée). Corrigé : fenêtre de stabilisation de 2 s après chaque LOAD.
- Après correction : erreur numérique en régime **±13 ms** ; seek TV détecté (`tv_jump`) et cuisine recalée en ~14 s (2 rechargements correctifs : chaque LOAD atterrit à ±65 ms).

## A22 — Scénario A complet sur CT 121 (11:07–11:28, `evidence/21-*`)
- **Hypothèse** : le relais (CT 121) suit seul une session YouTube pilotée comme par le téléphone (lecture, file d'attente, pause, seek, volumes, enchaînement, arrêt, nouvelle session).
- **Mesures** (`measure/analyze_session.py` → `evidence/21-session-report.json`) :
  - Vidéo 1, 344 s sans intervention : erreur numérique moyenne −1,4 ms, σ 12,3 ms, p95 17,5 ms, max 143 ms (1 échantillon, juste après le seek), dérive +16,7 ± 2,3 ms/10 min.
  - Seek TV → cuisine recalée en 14,6 s. Volume TV (0,15→0,10→0,15) et volume cuisine (0,05→0,03→0,05) : aucune perturbation (0,1–0,2 s, c.-à-d. jamais sortie de la synchro).
  - Pause/reprise : non jugeables (survenues pendant le remplacement à chaud du relais).
- **Échec** : à la fin de la vidéo 1 (+420 s), l'enchaînement automatique vers la vidéo 2 a bien été détecté (`content_changed`), mais la liaison CASTv2 avec le groupe était morte (timeout puis `BrokenPipe`, `SSL BAD_LENGTH`) et le relais **ne se reconnectait jamais** : 2026 erreurs en 10 min, cuisine muette pour la vidéo 2, l'arrêt et la nouvelle session.
- **Correction** : `GroupSink.reconnect()` (re-résolution du groupe par nom + garde), déclenchée après 2 erreurs consécutives ; démarrage automatique quand le groupe n'a plus de session ; le relais arrête la cuisine en quittant. Volumes restaurés exactement en fin de scénario (journal `volumes_restored`).
- **Suite** : scénario B = refaire pause/reprise, enchaînement forcé (seek à 570 s), vidéo 2 intacte 10 min, recommandation YouTube automatique (contenu inconnu, pubs possibles), arrêt, nouvelle session.

## A23 — Lien de groupe fermé par le récepteur : cause racine = heartbeat (11:31–11:49, `evidence/22-*`)
- **Constat** (essai B n°1, `22-relay-scenario-b-run1.jsonl`) : ~24 s après une pause TV, la liaison avec le groupe tombe (timeout, `BrokenPipe`). Pendant la pause le relais ne lit plus cette liaison → les PING du récepteur restent sans PONG → il ferme. Même cause probable que l'échec d'A22 (fin de vidéo).
- **Correction** : `CastLink.keepalive()` à chaque pas (lit, répond aux PING, envoie un PING toutes les 5 s) ; boîte de réception plafonnée (elle grossissait sans fin). Essai abandonné proprement (SIGINT → volumes restaurés). **Incident** : l'ancien relais a continué ~1 min, cuisine aux volumes normaux (0,55 sur 2 enceintes) — arrêté à la main, tout vérifié au repos.
- **Essai B n°2** (`22-relay-scenario-b.jsonl`, `22-session-report.json`) : **0 erreur de liaison** sur 15 min. Pause 15 s → recalé 8,6 s après la reprise ; seek → OK ; enchaînement auto V1→V2 suivi ; fin de file → TV `IDLE` → cuisine arrêtée ; nouvelle session suivie ; arrêt (`NO_APP`) suivi.
  - V2 : 413 s à −54,6 ms (σ par minute 5,5–8,8 ms après 2 min), dérive +4,1 ± 2,4 ms/10 min ; un saut de tête de lecture **côté TV** (t=423, deux lectures) → recalage à +60 ms, 172 s, dérive +0,9 ± 8,2.
  - Nouvelle session : +126 ms (dans la barre, mais les 2 corrections permises étaient épuisées).
- **Mesure de l'actionneur** : un LOAD atterrit à −76…−226 ms de l'horaire demandé (σ ≈ 50 ms) → c'est un actionneur grossier. Politique retenue : réessayer jusqu'à |erreur| < 50 ms (≤ 4 essais), biais appris amorti (gain 0,5).
- Pas de recommandation YouTube auto-lancée en fin de file (la TV reste sur la dernière vidéo, `IDLE`).

## A24 — Scénario C : vidéo ordinaire + 10 min intactes (11:50–12:07, `evidence/24-*`)
- Vidéo populaire `dQw4w9WgXcQ` (audio non préchargé) : téléchargé par yt-dlp sur le CT en **2,2 s**. **Bug** : le relais restait en `audio_pending` (il ne réexaminait le contenu qu'à un changement de vidéo) → corrigé (réévaluation à chaque pas tant que l'audio manque). **Aucune pub n'est apparue** : le comportement pendant les pubs reste non testé.
- V1 (QzomK1fdSUg) **593,5 s d'un seul tenant** : erreur numérique moyenne +28,4 ms, σ 12,9 ms, p95 43 ms, max 99 ms, **dérive +0,8 ± 1,1 ms/10 min** ; moyennes par minute 20–32 ms. Fin de file → arrêt cuisine.

## A25 — Actionneurs du groupe : la répétabilité décide (12:20–12:45, `evidence/25`, `26-*`, `27`)
- Après épuisement des corrections, une session pouvait rester à −109 ms ou +168 ms → ajout d'une correction d'entretien (≥ 80 ms, au plus toutes les 30 s).
- Test au démarrage (`26-*-run1`) : sur un lancement à froid, le relais mesurait l'erreur avant que la tête du groupe n'avance → corrections alternées −700/+800 ms. Corrigé : on n'évalue qu'après 2 intervalles d'avance à vitesse réelle.
- **Mesure décisive** (`probe/actuator_test.py`, haut-parleurs en sourdine, 8 essais chacun, `evidence/27-actuator-repeatability.json`) — dispersion de l'atterrissage :
  - A. LOAD à une position : moyenne −315 ms, **σ 202 ms** (dont lancement à froid −786) ;
  - B. SEEK en lecture : −169 ms, **σ 57 ms** ;
  - C. PAUSE → SEEK en pause → PLAY : −44 ms, **σ 30 ms**.
  → le relais **gare le groupe en pause** 1,6 s (SEEK) ou 3 s (LOAD) devant la TV et envoie PLAY à l'instant où la TV atteint cette position (avance apprise, départ 44 ms).
- En conditions réelles (`26-*-run3`) l'atterrissage est plus dispersé (~±80 ms) et **indépendant d'un essai à l'autre** : la même avance a donné +6/+37/−85 ms. Corriger un nouvel essai de l'erreur précédente doublait la variance (alternance ±100–170 ms). Politique finale : le biais s'apprend lentement (gain 0,15), chaque nouvel essai est un nouveau tirage avec le même biais, jusqu'à |erreur| < 50 ms (≤ 6 essais).
- Résultat (`26-*` run 4) : 6 seeks sur 7 recalés en 1–2 démarrages, médianes finales **−15…+23 ms** ; un cas a demandé 7 essais (+23 ms, max 93 ms).

## A26 — Session intacte de 11 min (13:26–13:39, `evidence/29-*`)
- **Hypothèse** : sans aucune intervention, l'alignement numérique tient sur une vraie session longue.
- **Mesure** : TV → `K0uxjdcZWyk` (carte de test 600 s), **aucune commande** pendant l'essai.
  Session de **690 s** ; plus longue plage ininterrompue **484 s** (une reprise de buffer TV vers
  t≈105 s coupe la plage, puis 93 s + 484 s) : moyenne **+7,7 ms**, σ 10,3 ms, p95 22 ms,
  max 77 ms, **dérive −5,5 ± 1,2 ms/10 min**. Recalage initial : 43 s (téléchargement + 2 atterrissages).
- **Conclusion** : critère « stable, sans dérive » **tenu côté numérique** sur > 10 min. L'offset
  **perçu** reste non mesuré (micro indisponible).

## A27 — Rollback testé avec valeurs archivées (13:40, `evidence/28-rollback-test.json`)
- Volumes abaissés avant l'essai, relais + YouTube lancés, puis **niveau 0** (arrêt du relais sur
  CT 121, `lib/emergency.py`, arrêt de la vidéo) : **11,2 s** pour revenir au silence, vérifié
  (`silent_after_level0 = true`), aucune session restante sur le groupe « Cuisine ».
- **Niveau 1** (`--restore`) : **écart nul** avec l'état d'avant (`volume_diffs = {}`) pour les 5
  Nest Audio et la TV. Volumes réels remis ensuite à la main (0,0833/0,55/0,55/0,0833/0,0833, TV 0,40).
- `lib/emergency.py` corrigé au passage : il **enregistre** les niveaux avant de muter (sinon
  `--restore` restaurait un état périmé) et découvre les groupes par mDNS (il ne connaissait pas « Cuisine »).
- **Conclusion** : procédure de rollback testée et chiffrée. Reste non testé : rollback pendant une
  lecture **au volume normal** (l'essai était à 0,05 / 0,10).

## A28 — Première mesure ACOUSTIQUE de l'offset perçu (2026-10-25 14:11–14:20)
Micro du téléphone entre la cuisine et la TV (`tools/phone_record.sh`, scrcpy, débogage sans fil
`192.168.2.148:39509`) ; la TV joue `QzomK1fdSUg` (bip 350 Hz/s), la cuisine joue sa piste de
clics 2310 Hz via `--measure-tracks`.
- **`delta = 0`** (`evidence/audio/31-probe.wav`, 40 s) : **+295 ms**, σ 5,6 ms, 36/40 paires
  retenues après filtrage MAD (4 paires aberrantes), MAD 2 ms, dérive ≈ 0. → la cuisine est
  ~300 ms **en retard**, ce qui confirme que `delta` n'est pas décoratif.
- **`delta = 0.295`** (`evidence/audio/23-acoustic-20261025-1414-2.wav`, minute 5) :
  **−30 ms, σ 3,3 ms, 57 paires** → **PASS** (seuil 150 ms), alias levé (famille ±3000 ms ±30).
- Deux fenêtres sur trois étaient **inutilisables**, et pour une raison identifiée : elles
  chevauchaient un recalage du relais (minute 1) ou le changement de vidéo (minute 10) → deux
  alignements mélangés, dispersion 94 ms et 204 ms. L'analyseur les a déclarées AMBIGUOUS, ce
  qui est le comportement voulu. `tools/acoustic_session.py` attend désormais un relais stable
  (`stable_since`) et enregistre à 1,5 / 5 / 8,5 min.
- **Correction instrument** : filtrage robuste (médiane + MAD) dans `measure/analyze_offset.py`,
  qui rejette les paires issues d'un bip manqué — c'est ce qui fait passer σ de 59 ms à 5,6 ms.
- **Réglage retenu** : `DELTA = 0.265` (0,295 − 0,030) dans `/etc/castrelay.conf`.
- **Non fait** : la série complète 3 mesures / ≥ 10 min sans tendance (2 fenêtres perdues, essai
  interrompu à la demande du propriétaire, l'essai étant gênant) et le contrôle de linéarité.

## A29 — Test avec de VRAIES vidéos (2026-10-25 14:33–14:45)
- Lien fourni n°1 (`36dBLWYkhCo`) : **pub non listée de 7,2 s** (« Shortened: AmzCA Evergreen2025
  Deal Notification ») — inutilisable comme test. Signalé au propriétaire, qui a fourni un vrai.
- Lien fourni n°2 : **`ai_gI0Jbr_E` — Pink Floyd, *Pulse*, 8630 s (2 h 23)**.
  - Téléchargement audio par le CT : **134 Mo en 10,2 s** (`content_ready`), cuisine qui démarre
    **13,6 s** après le changement de vidéo (dont le téléchargement).
  - Erreur **numérique** pendant 31 s de lecture : **médiane −30 ms**, max |e| 42 ms.
  - **Seek profond** TV → 7200 s : `tv_jump` détecté, cuisine recalée par SEEK en **3 essais
    (7205 → 7215 → 7224)**, médiane 11,9 ms ensuite ; un `prepare_failed` (retené).
    → un fichier de 2 h 23 est utilisable, y compris très loin dans la timeline.
  - Arrêt de la vidéo → `tv_session_ended`, cuisine arrêtée.
- **Mesure acoustique sur contenu réel : échec des deux méthodes essayées**, et c'est instructif :
  1. corrélation du micro avec l'audio de la vidéo, deux pics (TV + cuisine) → sur de la musique
     périodique les pics parasites dominent (0 ms vrai → 233 ms mesuré) ;
  2. autocorrélation de l'enregistrement (recherche de l'écho) → pics à 4–6 ms avec une
     corrélation de 0,07 : aucune détection crédible.
  **Conclusion** : sur du contenu arbitraire, l'offset perçu n'est pas mesurable sans marqueur.
  Le chiffre retenu reste celui de la carte de test : **−30 ms ± 3 ms** (A28). Une mesure sur
  contenu réel demanderait d'ajouter un marqueur connu (clics à 2310 Hz superposés à l'audio
  réel côté cuisine) — non implémenté, non indispensable tant que `delta` ne bouge pas.
- `measure/realcontent_offset.py` est conservé comme **outil expérimental non validé** (son
  auto-test échoue sur la musique) : ne pas l'utiliser pour conclure.

## A30 — Série acoustique de 5 points sur une session de 15 min (2026-10-25 15:32–15:47)
Protocole `notes/measurement-protocol.md`, `delta = 0.265`, TV 0.18 / groupe 0.30 (volumes bas
pour ne pas gêner), téléphone entre cuisine et TV, enregistrements de 60 s.

| moment | offset perçu | σ | paires gardées / rejetées |
|---|---|---|---|
| 0.75 min | **−20 ms** | 4.4 | 60 / 0 |
| 4.00 min | **−20 ms** | 4.5 | 59 / 0 |
| 7.25 min | **−20 ms** | 5.3 | 53 / 7 |
| 10.50 min | **+8 ms** | 3.5 | 16 / 4 |
| 13.75 min | **+34 ms** | 1.8 | 29 / 4 |

Session continue de **911 s (15.2 min)**, un seul app YouTube, changement de vidéo à 619 s.

- **Dans une même vidéo** (3 premiers points) : dérive **0.0 ± 0.0 ms/10 min**, étendue 0 ms.
  → critère « ≤ 150 ms à trois moments d'une session ≥ 10 min, sans tendance de dérive » **atteint**
  pour la session d'une vidéo (max |offset| 20 ms).
- **Au changement de vidéo** : marche de **+28 ms**, puis +26 ms sur les 3 min suivantes
  (max |offset| de la session : 34 ms, toujours ≤ 150).
  Interprétation : chaque nouvelle vidéo repart avec une latence de pipeline légèrement
  différente ; ce n'est pas une dérive de la boucle. À noter comme caractéristique connue.
- **Confirmation du « son qui s'arrête et repart 3-4 fois »** rapporté par le propriétaire : le
  journal montre 4 `sink_started` à 16.7 / 21.9 / 27.1 / 32.2 s, puis plus rien jusqu'à 498 s.
  C'est la boucle d'atterrissage qui réessayait (tolérance 50 ms, 6 essais) — chaque essai gare le
  groupe en pause ~1.6 s. **Corrigé** : tolérance 70 ms, **2 essais max**, seuil de correction
  grossière 350 ms, entretien à 110 ms toutes les 60 s, et le biais de démarrage appris est
  désormais **persisté** (`state/lead_adj.json`) pour que le premier vidéo après un redémarrage
  ne réapprenne pas.

## A31 — Volume et piste « appareil Cast » (même jour)
- **Miroir de volume** implémenté (`--mirror-volume`, `VOLUME_GAIN`, `VOLUME_MAX`) : le relais
  pousse le volume de la TV sur le groupe **sur front** (uniquement quand la TV change), pour ne
  pas se battre avec un réglage fait sur le groupe.
- Piste A reprise à la demande du propriétaire : `notes/fake-cast-device.md` — ce qui est mesuré
  (blocage à `deviceauth`), comment un fabricant obtient le certificat (programme Google Cast),
  les trois voies praticables, et l'expérience proposée avec le Chromecast de rechange
  (récepteur personnalisé, APP_ID à enregistrer — action propriétaire).

## A32 — Matrice de transport sur le service déployé (2026-10-25 16:10, `evidence/35-transport-matrix.json`)
Testé **contre le service systemd** (pas un relais lancé à la main), TV pilotée par `yt_tv` :

| # | Vérification | Résultat |
|---|---|---|
| 1 | lecture : la cuisine rejoint | **PASS**, 20.6 s (dont le téléchargement) |
| 2 | la TV joue bien la même vidéo | PASS |
| 3 | pause : la cuisine se met en pause | **PASS**, 6.3 s |
| 4 | reprise | **PASS**, 6.3 s, erreur numérique −66.8 ms |
| 5 | seek +7 min | **PASS**, mais **35 s de silence** — voir ci-dessous |
| 6 | seek −6 min | **PASS**, 6.3 s, erreur +65 ms |
| 7-8 | volume TV 0.22 puis 0.35 → groupe | **PASS** (0.22, 0.35) |
| 9 | arrêt : la cuisine s'arrête | **PASS**, 6.1 s |
| 10-11 | nouvelle session, autre vidéo | **PASS**, 20.6 s |

**Les 35 s du seek ne viennent pas du relais** : le journal ne montre rien entre `tv_jump` (158.5 s)
et le `sink_started` (194.8 s) parce que la **TV était en `BUFFERING`** — YouTube rebuffe après un
saut de 7 minutes. Le relais attend sagement ; il ne le disait pas. Corrigé : événement
`tv_waiting` toutes les 5 s.

**Effet du compromis « moins de coupures »** : l'erreur numérique en régime est passée de ±30 ms à
**−67…+65 ms** (tolérance d'atterrissage 70 ms, 2 essais). L'offset perçu = erreur numérique +
`delta` : il peut donc atteindre ~90 ms, toujours sous les 150 ms, avec **une seule coupure** au
démarrage au lieu de 4.

## A33 — Enchaînement automatique sur le service déployé (16:25)
Premier essai **invalide** : le test comparait l'URL tronquée à `K0uxjdcZWyk` alors qu'elle finit
par `.m4a` → faux positif immédiat. Corrigé (extraction de l'id par expression régulière) et
refait : TV → `K0uxjdcZWyk` enfilée avec `QzomK1fdSUg`, seek à 556 s, YouTube enchaîne seul, la
cuisine suit la **nouvelle** vidéo en **40 s** (dont ~10 s de recalage). `evidence/36`.
Leçon : un test dont le prédicat est faux passe toujours ; vérifier le prédicat avant de conclure.
