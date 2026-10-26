# Index des preuves

Chaque fichier de `evidence/` et ce qu'il vaut réellement. La relecture croisée
(`notes/cross-review.md`) a montré que plusieurs fichiers étaient cités pour des choses
qu'ils ne contiennent pas : cet index existe pour que ça ne se reproduise pas.

## Preuves solides, reproductibles

| Fichier | Contenu | Producteur |
|---|---|---|
| `01-discover-cast.txt` | 16 cibles Cast vues par mDNS, avec id/modèle/port | `tools/discover_cast.py` |
| `06-group-load-test.json` | un groupe accepte notre `LOAD`, tête de lecture 0 → 14.988 s, 5 Nest Audio exécutent le récepteur | `probe/group_load_test.py` |
| `08-groups-seek-rate.json` | 8 SEEK (erreur −410…−597 ms, ±93.8 ms), `SET_PLAYBACK_RATE` accepté | `probe/groups_seek_rate.py` |
| `09-group-membership-multizone.{json,log}` | **appartenance des 3 groupes** : tous contiennent la TV | `probe/group_membership.py` |
| `13-controller-sim.json` + `sim-*.jsonl` | boucle de contrôle en simulation, reproductible à ≤0.1 ms | `relay/sim.py` |
| `14-analyzer-selftest.json` | analyseur d'offset validé sur 10 cas piégeux, reproductible | `measure/selftest_analyzer.py` |
| `16-single-device-latency.json` | latence WAV vs M4A sur un Nest Audio (0.542 / 0.612 s) | `probe/single_device_latency.py` |
| `17-rate-gain-and-drift.json` | gain réel de `PLAYBACK_RATE` et dérive sur 600 s | `probe/rate_gain.py` |
| `11-virtual-receiver.log` | run corrigé : 12 connexions d'un expéditeur Google réel, 0 `CONNECT` | `probe/virtual_receiver.py` |
| `17-rate-gain-and-drift.json` | `PLAYBACK_RATE` sans effet ; dérive −12,4 ± 1,0 ms/10 min sur 600 s | `probe/rate_gain.py` |
| `18-group-video-test.json` + `18-tv-photo-1041.jpg` | un groupe Cast ne montre pas la vidéo (photo de la TV) | `probe/group_video_test.py` |
| `19-tv-playhead-dryrun.jsonl` | tête de lecture TV en dents de scie, résidu σ 55 ms (±140) | `relay/cast_relay.py --dry-run` |
| `20-relay-first-real.jsonl` | premier relais réel : +224 ms ± 9 ms, fin de vidéo suivie | idem |
| `21-*`, `22-*`, `24-*`, `26-*`, `29-*` | scénarios A (transport), B (pause/reprise, enchaînement, fin de file, nouvelle session), C (vidéo ordinaire + 10 min), D (atterrissages après seek), E (11 min intactes) + rapports `*-session-report.json` / `*-landing-report.json` | `tools/scenario.py`, `measure/analyze_session.py` |
| `23-acoustic-chain-selftest.json` | chaîne acoustique validée hors ligne : +6 ms de biais, σ 1 ms | mélange synthétique |
| `27-actuator-repeatability.json` | LOAD σ 202 ms, SEEK σ 57 ms, PAUSE→SEEK→PLAY σ 30 ms | `probe/actuator_test.py` |
| `28-rollback-test.json` | rollback niveaux 0/1 avec valeurs de volume avant/après | `tools/rollback_test.py` |
| `35-transport-matrix.json` | 11 vérifications de transport contre le **service** déployé | `tools/transport_matrix.py` |
| `36-auto-advance-service.json` | enchaînement automatique suivi par la cuisine, sur le service | test ad hoc, méthode décrite dans A32/A33 |
| `37-volume-vs-sync.json` | 4 changements de volume en lecture : miroirs exacts, 1 seul rechargement, erreur −19 ms | test ad hoc (A34) |
| `38-landing-50ms.json` | coût réel d'une tolérance de 50 ms : 1.25 rechargement/événement | test ad hoc (A34) |
| `39-landing-30ms.json` | tolérance 30 ms : 36 % des échantillons à ≤30 ms, médiane −5.3 ms, 2 rechargements/événement | test ad hoc (A35) |

## Preuves faibles ou ambiguës — à citer avec précaution

| Fichier | Problème |
|---|---|
| `04-raw-castv2-probe.json` | **échec** : `GET_STATUS` media → timeout. Ne prouve rien sur l'observation. |
| `05-device-snapshot.{json,log}` | `.log` et `.json` viennent de **deux exécutions différentes** (00:34 vs 00:37) et portent le même nom. Le `.log` contient **la seule** lecture réussie de la session TV (`t=1675.659`, `content=VyNgbQKORiA`). |
| `11a-virtual-receiver.log` | run d'origine, conservé tel qu'audité : `sender_disconnected` n'y est jamais écrit (bug de journalisation depuis corrigé). |
| `12-virtual-receiver-protocol.json` | **boucle locale** : notre client contre notre serveur (`127.0.0.1`). Valide notre encodage, pas la conformité. Le « ratio 1.0002 » est circulaire. |
| `15-panic-test.log` | montre l'état après arrêt d'urgence, **sans aucune valeur de volume** : ne prouve pas la restauration. |
| `07-seek-precision.log` | texte de console ; les deux premiers échantillons sont aberrants (`PLAYING` annoncé avant bufferisation). |
| `relay-20261025-011350.jsonl`, `…012111.jsonl` | **0 octet** : le relais n'a rien journalisé quand la TV ne jouait pas (corrigé depuis : battement de cœur). Conservés comme trace de l'échec. |

## Runs cassés — ne pas citer

| Fichier | Erreur |
|---|---|
| `02-cast-state.json` / `.log` | `AttributeError: 'MediaStatus' has no attribute 'media_info'` ; `get_chromecasts(known_hosts=…)` ignorait l'hôte demandé et renvoyait toujours le même appareil — d'où un nom d'appareil attribué au mauvais hôte. |
| `03-cast-state-by-host.log` | `UnboundLocalError` (bug de la bibliothèque) puis 3 `RequestTimeout`. |

Ces deux fichiers sont gardés uniquement parce que la règle du projet est de ne rien
supprimer. Ils ont été remplacés par `04`.

## Notes sur la fraîcheur

Plusieurs preuves sont des « runs » ponctuels sans horodatage interne. Pour les régénérer :
`tools/prove.sh` (tout ce qui est hors ligne) et les scripts `probe/*.py` (ce qui touche le LAN).

## v0.2.0 — preuves de la nuit du 2026-10-25 (aucune enceinte touchée)

Règle de cette nuit : **rien d'audible**. Le groupe « Cuisine » n'a reçu aucun paquet ; le
Chromecast `.80`, débranché de la TV par le propriétaire, n'a donc aucun chemin de sortie audio.

| Fichier | Contenu | Producteur |
|---|---|---|
| `40-resolve.json` | 3 vidéos réelles résolues sur CT 121 en **0,96–1,55 s** (budget 5 s), `avc1` 1080p + AAC 140, plafond 720p respecté | `probe/v020_media_probe.py` |
| `40-mpd.json` | le MPD synthétisé lu par `ffprobe` (deux pistes listées) | idem |
| `40-seek-7200.json` | seek à 7200 s : octets exacts comparés à la source | idem |
| `40-expiry.json` | mort forcée du flux en pleine réponse, récupérée dans la même réponse, **sha256 identique** | idem |
| `40-rss.json` | RSS **75,7 Mo** avec deux lecteurs concurrents (budget 150) | idem |
| `40-token-scan.txt` | aucun jeton `googlevideo` dans les fichiers archivés | idem |
| `41-preflight.json` | `.80` : `Backdrop`, inactif, volume 0,25 non muet, build Cast 1.56.291998, **uptime 17 h** | `tools/receiver_probe.py --probe-only` |
| `41-preflight-unavailable.json` | `28028CD2` → **`APP_UNAVAILABLE`**, `LAUNCH` → `NOT_FOUND` (l'app n'est pas encore connue de l'appareil) | idem |
| `41a-codec-matrix-dmr.json` | H.264 **joué** ; HEVC, VP9 (mp4 et webm), AV1 **refusés** | idem, backend DMR |
| `41b-rate-trim-dmr.json` | **`playbackRate` est honoré** : 0,998 → −0,001911 ± 0,000138 ; 1,002 → +0,002044 ± 0,000140 (3 séries) | idem |
| `41c-rate-stall-dmr.json` | un changement de cadence ne coupe pas la lecture (`PLAYING`, pas de retour en arrière de tête) | idem |
| `41y-page-selftest.json` | la logique de `receiver.html` exécutée hors ligne avec un CAF simulé : 33/33 | `receiver/selftest.js` |
| `41z-final-state.json` | `.80` laissé sur `Backdrop`, aucune app lancée | idem |
| `42-eureka-info-80.json` | lecture seule de `.80` (aucun lancement) | `tools/… --probe` (tâche D) |
| `42-sender-link-fake.json` (+ `.log`) | sur un récepteur CASTv2 local : erreur d'horloge **médiane 0,49 ms**, prise de contrôle détectée en **1 poll**, boîte de réception bornée | tâche D |
| `43-group-*` | estimateur de tête de lecture (médiane 5 échantillons, rejet, hystérésis 2 lectures) ; sonde `--dry-run` prouvée inerte ; suite + `prove.sh` | tâche E |
| `44-sim.json` | loi de commande simulée avec le bruit mesuré : p95 ≤ 5,4 ms (nominal), **0** `load`/`seek`/`play_at` en régime, 3 cas d'échec attendus et documentés | `tests/v020/sim_v020.py` |
| `45-relay-yield-offline.txt` | le relais v0.1 lit `state/group.owner` et **ne touche plus au groupe** quand v0.2 le possède | tâche H |
| `47-sender-selftest.json`, `47b`, `47c` | l'app propriétaire hors ligne : 43/43 cas HTTP, garde « zéro rechargement » prouvée contre un contrôleur hostile (2400 tics), 121 tests, `prove.sh` | tâche G |
| `48-*` | les instruments de la porte : analyseur (échec forcé sur un rechargement caché), acoustique (+6 ms de biais), transport, prise de contrôle étrangère, heures calmes, rollback — **auto-tests uniquement** | tâche J |

### Ce que ces fichiers ne prouvent pas

- Rien sur **notre page** de récepteur : elle n'a jamais pu être lancée (`APP_UNAVAILABLE`). Les
  chiffres de `41b`/`41c` sont ceux du lecteur **de la plateforme** sur le même appareil.
- Rien d'**audible** : aucun son n'est sorti pendant cette nuit, par construction.
- Aucune mesure **acoustique**, aucun transport réel, aucun rollback réel : la porte matérielle
  n'a pas été franchie.
