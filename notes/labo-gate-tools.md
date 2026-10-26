# Tâche J — les instruments de la porte, auto-testés ce soir

Branche `feat/gate-tools`, 2026-10-25. Aucun run matériel : les cinq outils de la porte sont
écrits et prouvés hors ligne (faux synthétiques, faux service en boucle locale, journaux v0.1
archivés). Les runs réels appartiennent à la porte matérielle.

Ce que cette note prouve, et avec quel chiffre. Tout est reproductible par
`evidence/48-run.log` (commandes + sorties + codes de retour).

---

## J1 — `measure/analyze_v020.py` : l'erreur numérique par fenêtre

**Hypothèse.** Un journal de session v0.2 (et, pour la régression, les journaux v0.1 archivés)
suffit à trancher les deux questions de la porte : chaque fenêtre de 5 s de régime a-t-elle une
médiane de `e = p_group − p_video − DELTA_V2` dans ±20 ms, et aucun `load`/`seek` n'a-t-il eu
lieu **dans** le régime ?

**Mesure** (`evidence/48-analyze-v020-selftest.json`, 9/9 cas conformes) :

| entrée | attendu | obtenu |
|---|---|---|
| journal v0.2 synthétique, biais +3 ms | PASS (0) | **PASS**, 41/41 fenêtres, pire \|médiane\| 5.5 ms, p95 9.1, max 13.3 |
| rechargement injecté à 120 s | violation (3) | **1** `load` en régime (73 s après l'événement précédent), 35 fenêtres |
| dérive injectée +60 ms/10 min | FAIL tolérance (1) | **FAIL**, 29/65 fenêtres hors ±20 ms |
| biais +35 ms | FAIL tolérance (1) | **FAIL**, 41/41 fenêtres hors ±20 ms |
| journal trop court (30 s) | indécidable (4) | **INCOMPLETE**, 0 fenêtre de régime |
| journal sans position vidéo + `--video-log` | PASS (0) | **PASS**, 836 échantillons appariés |
| entrée non-JSON | erreur d'entrée (2) | **réfutée**, exit 2 |

**Journaux v0.1 archivés** — ce que l'instrument en dit, sans maquillage :

- `evidence/29-relay-untouched.jsonl` → format `v0.1-relay`, **exit 3** :
  1 726 échantillons, 1 477 en régime, **96 fenêtres**, médiane globale **+8.5 ms**,
  p95 \|e\| **23.8 ms**, max **83.9 ms**, fenêtre la plus stable 317.4–322.0 s (médiane 4.7,
  σ 1.4). **8 fenêtres** dépassent ±20 ms (pire **31.2 ms**) et **un `realign`** du relais tombe
  73 s après le dernier événement de transport, donc **en plein régime** : c'est exactement ce
  que la v0.2 interdit (§1 : jamais de rechargement après le départ). Le journal v0.1 est
  donc « correctement rapporté » et **échoue la règle v0.2**, ce qui est attendu : il tournait à
  une tolérance d'atterrissage de 30–70 ms, pas à 20 ms.
- Dérive du plus long tronçon (84 fenêtres, 182–602 s) : **−3.7 ± 4.2 ms/10 min**, pas de
  tendance au-delà de 2σ. Le rapport archivé donne **−5.5 ± 1.2 ms/10 min** sur les
  échantillons bruts (4 Hz) : estimateur différent (médianes de 5 s vs échantillons), même
  conclusion. Les deux chiffres sont dans le rapport JSON.
- `evidence/39-landing-30ms.json` → format `v0.1-landing`, **exit 4 (INCOMPLETE)** : les quatre
  cas sont des `null` (délais dépassés), le fichier ne porte **aucun échantillon**. L'instrument
  refuse d'en fabriquer une série ; il rapporte les quatre médianes quand elles existent
  (`evidence/38` : 40.2 / 45.1 / 44.8 / 10.4 ms, et 38 échouerait ±20 ms).

**Conclusion.** L'instrument tranche les deux questions, distingue « pas de données » de
« données conformes » (exit 4 vs 0), et échoue sur un rechargement caché comme sur un dépassement
de ±20 ms. Il lit les deux formes v0.1 **par table de correspondance explicite** (imprimée par
`--print-schema`), jamais par devinement.

---

## J2 — `tools/acoustic_v020.py` : la série acoustique

**Hypothèse.** Trois fenêtres de 60 s à 1.5 / 5 / 8.5 min d'une même session ≥ 11 min, analysées
par `measure/analyze_offset.py` **inchangé**, avec le biais de chaîne **+6 ms** retranché
(`evidence/23-acoustic-chain-selftest.json`), suffisent à prononcer la porte acoustique — à
condition de jeter une fenêtre coupée par un rechargement (leçon A36).

**Mesure** (`evidence/48-acoustic-v020-selftest.json`, 15/15) :

- offsets vrais 0 / +40 / −60 ms → mesurés **−4 / +36 / −64 ms** (20 paires, σ 0.0) : la chaîne
  synthétique a un biais de **−4 ms**, sous les 6 ms de tolérance de l'auto-test ; le biais réel
  de +6 ms est une propriété des enveloppes TV 75 ms vs clic 30 ms, mesurée en `evidence/23`,
  et c'est pour cela que le pilote le **retranche** (vérifié : 40 → 34 ms).
- alias : un offset vrai de **+480 ms** est refusé (le vérificateur d'alias voit plus d'un
  candidat dans ±150 ms, et l'analyseur le déclare AMBIGUOUS).
- dispersion : gigue de ±120 ms → σ **113 ms** → refusée (seuil 40).
- paires : enregistrement de 4 s → **4 paires** → refusé (seuil 8).
- dérive : +40 ms/10 min injectée → retrouvée **40.0 ms/10 min** (tendance > 2σ) et acceptée
  (limite du protocole : 60 ms) ; +114 ms/10 min → **refusée**.
- linéarité §4.4 : +100 ms de DELTA → +98 ms mesurés → pente **0.98** (1:1 ±10 %) ;
  +10 ms → pente 0.10 refusée ; signe inversé → refusée.
- fenêtre coupée : un rechargement pendant l'enregistrement déplace `steady_since` → la fenêtre
  est **jetée**, pas moyennée.
- chaîne complète (partage → 3 enregistrements → analyse → verdict de série) avec un
  enregistreur factice : **PASS**, 3/3 fenêtres, dérive 0.0.

**Conclusion.** Le pilote refuse les quatre cas qui rendraient un chiffre faux (alias, σ > 40,
moins de 8 paires, fenêtre coupée) et applique le contrôle de linéarité du protocole. Il ne
mesure rien ce soir : aucune mesure acoustique réelle n'existe encore en v0.2.

---

## J3 — `tools/transport_v020.py` : la matrice de transport

**Hypothèse.** Les neuf comportements de `notes/ACCEPTANCE.md` §5 se vérifient contre le service
déployé par `POST /api/control` + `GET /api/status`, un verdict JSON et un temps par ligne.

**Mesure** (`evidence/48-transport-v020-selftest.json`, 9/9) : sur un faux service en boucle
locale, **12/12 lignes** passent, chacune avec sa durée ; quatre pannes injectées sont attrapées
— volume ignoré, reprise sans effet, `seek` ignoré, nouvel partage qui garde l'ancienne session —
ainsi qu'un `stop` qui laisse jouer. Sans `--duration-s`, la ligne « fin de média » est
**UNVERIFIED**, jamais PASS. Un service non-idle est refusé avant tout paquet.

**Conclusion.** La matrice ne peut pas passer à tort : chaque ligne doit voir l'état *et* le
mouvement (position qui avance, position gelée en pause, atterrissage à ±1.5 s). Deux limites
sont dites plus bas (volume, durée).

---

## J4 — `tools/foreign_takeover_v020.py` : la prise de contrôle étrangère

**Hypothèse.** Un second expéditeur (LAUNCH du Default Media Receiver depuis un processus jetable)
doit laisser le service en `idle`, groupe arrêté, **un** `device_lost`, sans relance pendant 30 s.

**Mesure** (`evidence/48-foreign-takeover-v020-selftest.json`, 8/8, dont un run de 30 s réel sur
le faux service) : service sain → **PASS**, `idle` 0.0 s après la prise, 1 `device_lost`, groupe
arrêté, aucune relance sur 30 s. Quatre pannes sont attrapées : service qui continue de jouer,
**deux** `device_lost`, relance à +0.8 s (état et journal), groupe qui joue encore. Une fenêtre
d'observation de 2 s donne **INCOMPLETE** (« les quatre conditions tiennent sur 2 s, le contrat en
demande 30 »), jamais PASS. Le mode réel **refuse de lancer** sans `--yes-takeover`.

Le second expéditeur est bien un **processus jetable** (`--throwaway-launch HOST APP`) lancé par
le parent : sur la boucle locale il rend un JSON et sort en 1 (connexion refusée), ce qui prouve
le mécanisme sans toucher aucun appareil (section 6b de `evidence/48-run.log`).

**Conclusion.** Le détecteur est juste et le mode réel est verrouillé : rien ne sera lancé sur
`.80` sans une décision explicite du propriétaire.

---

## J5 — `tools/quiet_v020.py` : les heures calmes

**Hypothèse.** Une fenêtre de ~3 min autour de maintenant, écrite dans la configuration puis
service redémarré, produit `quiet_refused` **sans un seul paquet au groupe** ; avec
`video_only`, la vidéo joue et le groupe reste intact ; la configuration est restaurée à
l'octet.

**Mesure** (`evidence/48-quiet-v020-selftest.json`, 7/7) : chemin sain complet (refus + zéro
paquet + vidéo qui avance + zéro paquet + configuration identique après restauration) ; cinq
pannes attrapées : partage qui joue malgré les heures calmes, paquet groupe pendant le refus,
`load` groupe sous `video_only`, vidéo qui ne joue jamais, **restauration qui ne prend pas
effet**. `--dry-run` n'écrit rien (fichier identique).

**Conclusion.** Les deux politiques sont vérifiées, et la restauration est **prouvée**, pas
supposée : relecture du fichier octet par octet + `/healthz` + `quiet=false`.

---

## J6 — `tools/rollback_v020.py` : niveaux 0 et 1

**Hypothèse.** La méthode A27 (`evidence/28`) s'étend à la v0.2 : arrêt, silence, vérification,
restauration, comparaison stricte des volumes, **y compris l'application récepteur sur `.80`**.

**Mesure** (`evidence/48-rollback-v020-selftest.json`, 8/8) : flotte en mémoire, mêmes fonctions
`compare()` et `silent_after()` que le run LAN. Chemin sain → PASS, 5 enceintes + TV comparées
sur `(niveau, mute)`, récepteur arrêté. Quatre pannes attrapées : restauration qui rate une
enceinte, groupe laissé en lecture, récepteur laissé sur `.80`, restauration qui **touche la TV**.
Une enceinte déjà mutée avant l'essai le reste après. Aucune session → **REFUSED** sans un seul
appel.

**Conclusion.** Le rollback ne peut pas « passer » en changeant un volume ni en laissant le
récepteur tourner. Le run réel (arrêt du service, `lib/emergency.py`) reste à faire.

---

## Ce qui n'est PAS prouvé, et pourquoi

1. **Aucun run matériel.** Les cinq outils de porte n'ont pas tourné ce soir : fenêtre audio
   fermée, Chromecast `.80` réservé à la tâche récepteur, `/etc/castsender.conf` non modifié.
   Chaque `--help` imprime la précondition exacte du propriétaire.
2. **Le biais synthétique (−4 ms)** de l'auto-test acoustique n'est pas le biais réel (+6 ms) :
   il dépend des formes d'enveloppe. Le pilote applique +6 ms (mesuré, `evidence/23`) ; l'auto-test
   vérifie l'arithmétique (40 → 34) et la récupération à ±6 ms.
3. **`evidence/29` échoue la règle v0.2** (±20 ms et aucun rechargement en régime). C'est le
   résultat attendu d'un journal v0.1 (tolérance 30–70 ms, `realign` permis) ; ce n'est pas une
   preuve que la v0.2 échouera.
4. **`evidence/39` ne contient aucun échantillon** : l'instrument le dit (exit 4) au lieu
   d'inventer une série.
5. **Le journal d'événements du service n'est pas défini par le contrat** : les outils 4, 5 et 6
   lisent `--events-log` (défaut `/opt/castsender/state/events.jsonl`) et le disent quand le
   fichier manque (repli sur le sondage de `last_event`, qui peut rater un doublon).
6. **Le volume du groupe n'est pas exposé par `GET /api/status`** : la ligne volume de la matrice
   lit le groupe en **lecture seule** (CASTv2 `GET_STATUS`) ; le repli sur un champ
   `group_volume` est documenté comme provisoire.
7. **La fin de média exige `--duration-s`** : `/api/status` ne publie pas la durée. Sans elle, la
   ligne est UNVERIFIED (jamais PASS).

## Amendements proposés à `docs/v020-contracts.md` (aucune modification faite)

1. **§5 — journal d'événements.** Ajouter le chemin et la forme du journal de session
   (`{"wall", "type", "side", "media"?, "group_media"?, "video_media"?, "delta_v2"?}`), plus
   `share`, `first_frame`, `load`, `seek`, `play_at`, `play`, `pause`, `rate`, `stop`, `ended`,
   `device_lost`. Sans lui, « un `device_lost` » et « aucun rechargement en régime » ne sont pas
   auditables.
2. **§5 — volume.** Ajouter `group_volume` (et `group_muted`) à `GET /api/status`, ou acter que la
   porte lit le groupe en CASTv2 lecture seule.
3. **§5 — durée et fin de média.** Ajouter `duration_s` à `GET /api/status` (nécessaire pour
   juger « fin de média » sans paramètre externe).
4. **§7 — surface du refus.** Dire si un partage refusé répond `202 {"state":"quiet_refused"}` ou
   `409` : le contrat nomme l'état mais pas le code HTTP. L'outil accepte les deux et **rapporte
   laquelle** il a vue.
5. **Protocole §5 (dérive).** La ligne « `|drift_projected_10min| ≤ 60 ms` **et** `|valeur| > 2σ` »
   se lit mal : nous l'appliquons comme « ≤ 60 ms, ou une tendance non distinguable de zéro
   (≤ 2σ) ». À reformuler.

## Commandes (tout est hors ligne)

```sh
cd ~/opencode/nestaudiocast-wt/gate

# 0. suites
./tests/v020/run.sh                 # 36 tests, dont tests/v020/test_analyze_v020.py (24)
./tools/prove.sh --fast             # vert (lecture seule sur le LAN)

# 1. instruments
.venv/bin/python measure/analyze_v020.py --self-test --json evidence/48-analyze-v020-selftest.json
.venv/bin/python measure/analyze_v020.py evidence/29-relay-untouched.jsonl \
    --json evidence/48-analyze-v020-29.json      # exit 3 (attendu)
.venv/bin/python measure/analyze_v020.py evidence/39-landing-30ms.json \
    --json evidence/48-analyze-v020-39.json      # exit 4 (attendu)

# 2-6. les cinq outils de la porte, auto-test seulement
.venv/bin/python tools/acoustic_v020.py    --self-test --json evidence/48-acoustic-v020-selftest.json
.venv/bin/python tools/transport_v020.py   --self-test --json evidence/48-transport-v020-selftest.json
.venv/bin/python tools/foreign_takeover_v020.py --self-test \
    --json evidence/48-foreign-takeover-v020-selftest.json
.venv/bin/python tools/quiet_v020.py       --self-test --json evidence/48-quiet-v020-selftest.json
.venv/bin/python tools/rollback_v020.py    --self-test --json evidence/48-rollback-v020-selftest.json
```

Les cinq outils de porte ont aussi `--preflight` (lecture seule) pour la porte matérielle, et
chacun installe, dans `--self-test`, un garde de socket qui **refuse toute connexion non locale**
(« offline by construction »). Ce garde a effectivement attrapé une fuite pendant l'écriture de
`rollback_v020.py` ; elle est corrigée.
