# Labo — relais v0.1 : céder le groupe à v0.2 (tâche H, 2026-10-25)

Branche `feat/relay-yield`, worktree `~/opencode/nestaudiocast-wt/relay-yield`.
Tout ce qui suit est **hors ligne** : aucun paquet n'est parti vers le groupe Cuisine, la TV ou le
Chromecast. Le seul accès LAN est en **lecture** (statut du groupe, unité systemd, sommes de
contrôle sur CT 121). Le test matériel n'a **pas** été exécuté : il est écrit plus bas, marqué, et
demande la fenêtre du propriétaire.

## 1. Le problème, tel qu'il est prouvé

Deux chemins v0.1 touchent le groupe Cuisine quand le relais s'arrête ou redémarre :

1. `ExecStopPost` lance `lib/emergency.py`, qui **mute les 5 Nest Audio** (`mute_everything()`)
   et **arrête toute application en cours sur un groupe sûr** (`clean_sessions()`).
2. `GroupSink.connect()` **démute** les membres au démarrage (le correctif de
   `notes/consensus.md` §5bis.2).

Les deux sont justes tant que v0.1 est seul. Sous une session v0.2 vivante, le mute ne fait pas de
bruit mais **rend muette la cuisine que v0.2 joue**, et `clean_sessions()` arrête l'application DMR
de v0.2 — c'est la classe d'incident que la tâche supprime.

**Hypothèse H1** — si le relais lit le verrou d'appartenance de `docs/v020-contracts.md` §7 avant
chaque poll, et si le chemin d'arrêt consulte le même verrou avant de muer, alors v0.1 ne peut plus
toucher le groupe pendant que v0.2 le possède, **sans** changer un seul autre comportement v0.1.

**Mesure** — sections 3 à 6. **Conclusion** — section 8.

## 2. Ce qui a été livré

| Fichier | Rôle |
|---|---|
| `relay/cast_relay.py` | `GroupOwner` + `read_group_owner()` (lecture seule du verrou) ; `Relay._lock_check/_yield_tick/_resume_from_yield` ; `--owner-file` ; connexion et arrêt du groupe conditionnés au verrou |
| `tools/relay-stop-post.sh` (nouveau, 0755) | `ExecStopPost` : si un propriétaire v0.2 est vivant, **rien** n'est lancé et `skip_mute_v020` est journalisé ; sinon `lib/emergency.py`, inchangé |
| `tools/run-relay.sh` | passe `--owner-file ${OWNER_FILE:-state/group.owner}` (défaut = contrat) |
| `tools/prove.sh` | §5 : trois gardes statiques ajoutées, contrôles existants et libellés intacts |
| `tests/v020/test_relay_yield.py` | 12 tests, pilote le parseur seul (verrou frais, périmé, malformé, absent, le nôtre) |
| `notes/labo-relay-yield.md` | ce fichier |

## 3. Le verrou, tel que le relais le lit

`read_group_owner(path, now, self_pid)` renvoie une lecture figée : `present`, `ok`, `owner`,
`pid`, `since`, `age_s`, `stale`, `v020`, `self_owned`, `reason`.

| Cas | `ok` | `stale` | `v020` | Comportement du relais |
|---|---|---|---|---|
| `{"owner":"cast-sender","pid":N,"since":T}` frais (< 15 s) | oui | non | **oui** | ne touche à rien, bat, reprend seul |
| même fichier, battement > 15 s | oui | oui | non | v0.1 normal (reprise autorisée par §7) |
| malformé (JSON cassé, mauvais type, propriétaire inconnu) | non | oui | non | v0.1 normal |
| absent | non | oui | non | v0.1 normal |
| `{"owner":"cast-relay","pid":<le nôtre>}` | oui | non | non (`self_owned`) | v0.1 normal |
| illisible (droits, répertoire à la place du fichier) | non | oui | non | v0.1 normal |

Deux décisions de sémantique, mesurées par les tests :

- **Fraîcheur = `max(mtime, since)`.** Le contrat dit « rafraîchi comme battement toutes les 5 s » ;
  il ne dit pas si `since` est l'heure de création ou celle du dernier battement. Une réécriture
  atomique est un battement : le `mtime` suffit, et `since` s'il avance aussi. Un fichier vieux des
  deux côtés est périmé.
- **Un `since` dans le futur compte comme frais.** C'est une horloge décalée, pas une preuve de
  péremption ; le sens sûr est de laisser le groupe tranquille.

**Lecture seule, prouvée** : `open(..., "rb")`, aucun `O_EXCL`/`O_CREAT` dans `cast_relay.py`
(garde statique de `prove.sh`), et le test `test_reading_leaves_the_file_byte_identical` vérifie que
trois lectures laissent `mtime` et contenu identiques. §7 donne l'écriture au propriétaire : un
relais qui écrirait ce fichier entrerait en course avec le battement de v0.2 et mettrait son propre
pid dans `pid`.

## 4. Comportement du relais pendant la cession

Pendant que `v020` est vrai, `Relay.step()` sort **avant** tout le reste :

- aucun `LOAD`, `SEEK`, `PLAY`, `PAUSE`, `SET_PLAYBACK_RATE`, `SET_VOLUME` — ni mute, ni **démute**
  (c'est pourquoi `main()` ne fait même pas `sink.connect()` au démarrage si le verrou est vivant :
  son démute des membres muets est un paquet de trop) ;
- aucun téléchargement `yt-dlp`, aucun miroir de volume (`_sync_volume` est après la garde) ;
- il **continue de poller** la TV et le verrou, et écrit un battement `relay_yielded` dans son
  journal au plus toutes les 5 s (`YIELD_LOG_S`).

Au relâchement (verrou levé ou périmé), `_resume_from_yield()` **jette l'état local** de session
(`session`, `transport`, `content`, `state`) au lieu de l'hériter : un `SEEK` sur un identifiant de
session disparu — ou pire, sur celle de v0.2 — est exactement ce qu'il faut éviter. Il reconnecte
le groupe si le lien est absent ou fermé (une cession peut durer des heures), puis le chemin
`need_start` existant recharge notre propre média.

Au **démarrage**, même règle : si le verrou est vivant, le relais annonce `starting idle, polling
only` et n'ouvre pas le lien vers le groupe.

À l'**arrêt**, `main()` relit le verrou : si v0.2 possède le groupe, il **n'appelle pas**
`sink.stop()` (l'application qui joue là est celle de v0.2) ; il ferme seulement son propre lien,
ce qui n'est pas une commande pour le groupe.

**Risque résiduel, non mesuré** : si le relais jouait déjà quand v0.2 prend le verrou, il s'arrête
de parler sans arrêter sa session — c'est volontaire (un `PAUSE`/`stop_app` envoyé à ce moment
pourrait tuer la session que v0.2 vient d'ouvrir). On compte sur le `LOAD` de v0.2 sur le même
récepteur DMR pour remplacer la nôtre. Si un jour v0.2 prenait le verrou **sans** charger le groupe
(politique `quiet hours = video_only`), jusqu'à une phrase de notre audio pourrait continuer. À
vérifier dans la fenêtre matérielle, pas prouvé ici.

## 5. Pourquoi la suppression du mute vit dans le wrapper

Contrainte : `lib/emergency.py` est **gelé** (contrat §0) et partagé avec le chemin de panique
manuelle ; il ne lit aucun drapeau d'environnement. Vérifié sur la boîte :

```
$ ssh root@192.168.2.21 'sha256sum /opt/castrelay/lib/emergency.py'
fcc8a4287fa5ab127bde2cc6f95c6550a2c09d4c56d0e3f98731472cb8758175  /opt/castrelay/lib/emergency.py
$ sha256sum lib/emergency.py
fcc8a4287fa5ab127bde2cc6f95c6550a2c09d4c56d0e3f98731472cb8758175  lib/emergency.py
```

Le déployé est identique au dépôt, sans drapeau : la seule voie sans éditer `lib/` est donc de
décider **avant** l'appel. L'endroit naturel de cette décision est l'unité systemd, pas le code du
relais : c'est elle qui décide *si* le crochet d'arrêt tourne, et la changer est réversible d'une
ligne, sans redéploiement de code.

Choix retenu : **sauter l'appel entier** et journaliser `skip_mute_v020`, plutôt que d'importer
`emergency` et d'appeler `stop_our_processes()` à la main.

Justification : le mute n'est pas la seule étape nuisible. `clean_sessions()` arrête toute
application en cours sur un groupe qui passe la garde — Cuisine la passe — donc il arrêterait la
session DMR de v0.2. « Supprimer le mute » en gardant `clean_sessions()` laisserait exactement la
moitié destructrice du problème. Et le nettoyage de processus est déjà fait par systemd au moment
où `ExecStopPost` tourne (`TimeoutStopSec=20`), tandis que le relais lui-même arrête sa session dans
son `finally` quand le verrou est libre.

En cas d'échec du **contrôle** (import impossible, venv cassé), le wrapper retombe sur le
comportement v0.1 et le dit (`v0.1 stop path: lock unreadable (v0.1 behaviour)`). C'est le sens
sûr : ne pas conclure « v0.2 possède » sur une erreur de lecture, mais ne pas non plus inventer une
cession qui n'a pas été prouvée.

### Diff exact de l'unité (à appliquer par l'intégrateur, pas par moi)

État mesuré sur CT 121 le 2026-10-25 (`systemctl cat cast-relay`) :

```ini
ExecStart=/opt/castrelay/tools/run-relay.sh
# If the relay dies, silence the kitchen rather than leave a runaway session.
ExecStopPost=-/opt/castrelay/.venv/bin/python /opt/castrelay/lib/emergency.py
```

Ligne à mettre (une seule) :

```ini
ExecStopPost=-/opt/castrelay/tools/relay-stop-post.sh
```

puis `systemctl daemon-reload` (pas de redémarrage : l'unité n'est relue qu'au prochain arrêt).
Le wrapper trouve `/opt/castrelay` tout seul (`dirname $0/..`) ; `CASTRELAY_ROOT` et
`GROUP_OWNER_FILE` existent seulement pour les tests hors boîte.

**Ordre de déploiement (important)** : `relay/cast_relay.py` d'abord — `--owner-file` y est
optionnel avec la bonne valeur par défaut, donc l'ancien `run-relay.sh` déployé continue de
marcher ; puis `tools/run-relay.sh` et `tools/relay-stop-post.sh` ; enfin la ligne d'unité.
Dans l'autre sens (script avant code), le service refuserait l'argument inconnu.

## 6. Mesures hors ligne

### 6.1 Suite v0.2

```
$ ./tests/v020/run.sh
Ran 24 tests in 0.003s
OK
```

12 tests nouveaux (`test_relay_yield`), 12 existants (`test_contracts`) inchangés.

Note : `./tests/v020/run.sh test_relay_yield` (chemin « un seul module ») est cassé **avant** ma
modification — `python -m unittest -v discover -s …` passe `discover` comme nom de test. Je n'ai
pas touché `tests/v020/run.sh` (hors de mes fichiers) ; la suite complète le remplace :
`.venv/bin/python -m unittest -v discover -s tests/v020 -p test_relay_yield.py`.

### 6.2 Gardes statiques

```
$ ./tools/prove.sh --fast
...
== 5. safety regressions (offline, static) ===========
   ok: silencing mutes only, never zeroes levels
   ok: the relay undoes its own stop-mute at startup (per member)
   ok: member uuid -> IP mapping present (needed to unmute members directly)
   ok: warns when every group member is silent
   ok: the relay reads state/group.owner and yields the group to a live v0.2 owner
   ok: the lock is read-only for the relay (never created, refreshed or deleted)
   ok: ExecStopPost wrapper skips emergency.py while v0.2 owns the group
   -> all safety regressions clear
```

### 6.3 Le relais ne parle pas au groupe (sink factice)

Le relais réel, avec un sink en mémoire qui **compte chaque paquet** (`connect`, `prepare`,
`play`, `pause`, `seek`, `set_volume`, `stop_app`, `keepalive`), un verrou frais, et
`mirror_volume=True` :

```
{"phase": "yielded", "steps": 4, "group_calls": [], "owner_v020": true}
{"phase": "yielded", "log_events": ["relay_yielded"]}
{"phase": "heartbeat", "group_calls": [], "relay_yielded_lines": 2,
 "last": {"ev": "relay_yielded", "owner": "cast-sender", "pid": 999999, "age_s": 6.2,
          "reason": "fresh", "note": "v0.2 owns the group: no group traffic"}}
{"phase": "lock_stale", "group_calls": [], "yielded": false}
{"phase": "after_resume", "group_calls": ["link.keepalive", "set_volume", "yt-dlp", "stop_app"],
 "yielded": false, "need_start": true}
```

**Nombre qui compte : 0 paquet vers le groupe sur 5 polls cédés**, y compris le miroir de volume,
avec 2 battements à 5,2 s d'intervalle. Après péremption : 0 paquet pour la reprise elle-même
(l'état local est jeté), puis le comportement v0.1 normal au poll suivant.

### 6.4 Le wrapper décide correctement

Exécuté cinq fois avec `lib/emergency.py` remplacé par un **stub** qui n'imprime qu'une ligne
(aucun haut-parleur joignable dans le `ROOT` de test) :

| Verrou | Sortie du wrapper | `emergency.py` |
|---|---|---|
| `cast-sender` frais | `skip_mute_v020 owner=cast-sender pid=999999 age_s=1.138` | **non lancé** |
| `cast-sender` périmé (40 s) | `v0.1 stop path: owner=cast-sender reason=stale age_s=40.133` | lancé (stub) |
| malformé | `v0.1 stop path: owner=None reason=malformed:not-json age_s=None` | lancé (stub) |
| absent | `v0.1 stop path: owner=None reason=absent age_s=None` | lancé (stub) |
| `cast-relay` frais (le nôtre) | `v0.1 stop path: owner=cast-relay reason=fresh age_s=1.132` | lancé (stub) |

Sur le vrai `ROOT`, le même contrôle donne `absent` (CT 121 n'a **pas** de
`/opt/castrelay/state/group.owner` aujourd'hui : `ls` le 2026-10-25), donc v0.1 garde son
comportement actuel jusqu'à la première cession v0.2.

## 7. Le test LAN que je n'ai pas fait (écrit, exécutable, **NON EXÉCUTÉ**)

À faire dans la fenêtre matérielle du propriétaire, quand le son est permis. Reste hors ligne tout
ce qui peut l'être ; chaque étape dit ce qu'elle prouverait.

```sh
# 0. préalables : v0.1 en service, personne ne joue, fenêtre sonore ouverte.
ssh root@192.168.2.21 'systemctl is-active cast-relay; ls -l /opt/castrelay/state/'

# 1. Une fausse cession v0.2 : verrou frais, pid bidon. NE PAS toucher au groupe.
ssh root@192.168.2.21 'python3 - <<PY
import json, time
json.dump({"owner":"cast-sender","pid":999999,"since":time.time()}, open("/opt/castrelay/state/group.owner","w"))
PY
date +%s'

# 2. Le crochet d'arrêt doit refuser de muer — et le dire.
ssh root@192.168.2.21 '/opt/castrelay/tools/relay-stop-post.sh; echo rc=$?'
#    attendu : skip_mute_v020 owner=cast-sender pid=999999 age_s=…
#              "lib/emergency.py NOT run: …"  et AUCUNE ligne de mute.

# 3. Preuve d'absence de paquet : les 5 enceintes n'ont pas bougé.
ssh root@192.168.2.21 'cd /opt/castrelay && .venv/bin/python lib/silence.py'

# 4. Redémarrage du relais sous verrou : il doit démarrer inerte.
ssh root@192.168.2.21 'systemctl restart cast-relay;
  sleep 5; journalctl -u cast-relay -n 20 --no-pager | grep -E "v0.2 owns|relay_yielded"'
#    attendu : "v0.2 owns the group (cast-sender pid=999999 …): starting idle, polling only"
#              puis un relay_yielded toutes les ~5 s, et AUCUN group guard OK
#              (le lien vers le groupe n'est même pas ouvert).

# 5. Retirer le verrou (déplacement, jamais de suppression) et vérifier la reprise :
ssh root@192.168.2.21 'mkdir -p /opt/castrelay/evidence/archive &&
  mv /opt/castrelay/state/group.owner /opt/castrelay/evidence/archive/group.owner.test-H;
  sleep 3; journalctl -u cast-relay -n 20 --no-pager | grep -E "relay_resumed|sink_started"'
#    attendu : relay_resumed, puis un démarrage normal si la TV joue du YouTube.
```

Ce que ce test prouverait : (a) le crochet d'arrêt ne mute pas sous verrou v0.2 ; (b) le relais
démarre sans ouvrir le lien groupe ; (c) il reprend seul au relâchement. Ce qu'il ne prouve pas :
le comportement réel d'une session v0.2 simultanée (il faut la tâche de l'expéditeur).

Pour la reproductibilité hors ligne des §6.3/§6.4, voir `evidence/45-relay-yield-offline.txt`, qui
contient les scripts et leurs sorties.

## 8. Conclusion

H1 est **vérifiée hors ligne** : lecture seule du verrou, zéro paquet vers le groupe pendant la
cession, reprise automatique, et un chemin d'arrêt qui ne mute plus. Aucun autre comportement v0.1
n'est modifié : sans verrou (état actuel de CT 121), le relais et `ExecStopPost` font exactement ce
qu'ils faisaient.

**Non prouvé, et il faut le dire** :

1. le comportement réel sous une session v0.2 vivante (test §7, non exécuté : le son de la cuisine
   est interdit cette nuit) ;
2. que le `LOAD` de v0.2 remplace toujours notre ancienne session si le relais jouait au moment de
   la prise du verrou (risque résiduel §4) ;
3. l'effet d'une cession de plusieurs heures sur le lien CASTv2 du groupe (le relais ne le garde
   plus en vie pendant la cession ; il le reconnecte à la reprise, mais ce chemin n'a pas été
   exercé sur le matériel) ;
4. que `state/group.owner` sera bien créé au chemin par défaut : `--owner-file` vaut
   `state/group.owner` relatif à `WorkingDirectory=/opt/castrelay`, ce que le wrapper résout en
   absolu ; les deux chemins coïncident sur la boîte, ils n'ont pas été comparés en marche.

## 9. Amendement proposé aux contrats (non commité)

§7, première puce, après « heartbeat only » :

> The relay reads `state/group.owner` **read-only** (`relay.cast_relay.read_group_owner`): it never
> creates, refreshes or deletes it, yields only to `{"owner":"cast-sender"}` with a heartbeat
> younger than 15 s (freshness = `max(mtime, since)`), treats absent/malformed/unreadable as "no
> v0.2 owner", sends no LOAD/SEEK/PLAY/PAUSE/mute/unmute/volume packet while yielded, opens no
> group link at startup, and calls no `sink.stop()` on shutdown; the stop hook
> (`ExecStopPost=tools/relay-stop-post.sh`) runs `lib/emergency.py` only when no live v0.2 owner
> exists, otherwise logs `skip_mute_v020`.

## 10. Écart de documentation relevé

Le brief de la tâche parle de « A37 incident » pour le mute sous session v0.2. Dans
`notes/00-labo.md`, **A37 est la série acoustique à 30 ms** (17:11) et ne décrit aucun incident de
mute. Le mécanisme réellement documenté est `notes/consensus.md` §5bis.2 (le service restait muet
après redémarrage) et `ACCEPTANCE.md` §7. Aucun incident « mute pendant une session v0.2 » n'est
consigné : il est donc **plausible et non observé**, ce qui ne change pas la correction mais change
ce qu'on peut en affirmer.
