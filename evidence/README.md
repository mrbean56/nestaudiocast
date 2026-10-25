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
