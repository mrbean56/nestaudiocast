# Cahier de laboratoire — tâche G, l'app propriétaire (squelette du sender)

Branche `feat/sender-skeleton`, worktree `~/opencode/nestaudiocast-wt/sender`.
Règle du projet : **hypothèse → mesure → conclusion**. Rien ici ne touche une enceinte, le
groupe « Cuisine », la TV ou le Chromecast : tout tourne sur les fakes de `tests/v020/fakes`,
en mémoire, sur `127.0.0.1`. Les seuls chiffres cités viennent des commandes de la §8.

## Ce que la tâche livre, en une ligne

`sender/` = l'app propriétaire de `docs/v020-contracts.md` §5-§8 : `api.py` (un
`ThreadingHTTPServer` LAN-only), `session.py` (la machine à états et **la garde « pas de
rechargement après le départ »**), `quiet.py`, `group_lock.py` (+ `boot_cleanup`), `log.py`
(JSONL + `redact()`), `config.py`, `offline.py` (fakes), `service.py` (entrée), `selftest.py`
(la preuve hors ligne).

---

## G1 — La matrice de requêtes HTTP (§5)

**Hypothèse** : les 6 routes de §5 suffisent, et chaque entrée mauvaise doit répondre un JSON
`{"error": …}` avec le bon code, sans jamais laisser un client sans réponse.

**Mesure** (`python -m sender.selftest`, bloc `http_matrix`) : **43 cas sur 43** passent sur un
vrai socket `127.0.0.1` — 200/202 nominales, 400 (corps vide, JSON invalide, tableau, `url`
absente ou non-http, `start` négatif, champ inconnu), 403 (client hors `192.168.2.0/24`), 404,
405 (`GET` sur `/api/share`, `POST` sur `/api/status`, `DELETE /`, `HEAD`), 410 (sid révoqué),
413 (corps > 64 Kio), 415 (`text/plain`), 501 (`/media/<sid>/audio.m4a` sans hook), et les
trois chemins non-JSON : `/` (page autonome, **zéro** `http://`/`https://`/CDN), `/receiver.html`
(404 tant que `receiver/` n'existe pas, 200 dès qu'il existe), `/media/<sid>/manifest.mpd`.

**Conclusion** : la surface de §5 est couverte, y compris le refus hors LAN (vérifié sur le
socket, pas seulement en unité). Deux ajouts nécessaires, proposés en amendement (§9 plus bas) :
`/assets/measure/*` (pour `measure: true`) et un hook `MediaProxy.serve_http` (pour les
endpoints d'octets de §4).

## G2 — La session de 10 minutes : **zéro** `load`/`seek` après le départ

**Hypothèse** : l'acceptation « pas de trou après le départ » ne doit pas dépendre de la bonne
conduite de la loi de commande (tâche F) : elle doit être **impossible à violer** depuis la
session, même avec un contrôleur hostile.

**Mesure** (bloc `no_reload.hostile_controller`) : `HostileController` demande `load`, `seek`
**et** `play_at` à **chacun** des **2400** tics (600 s simulées à 0,25 s). Résultat :
`load_after_start = 0`, `seek_after_start = 0`, `play_at_after_start = 0`,
`prepare`/`seek` groupe = 0, et **60** lignes `reload_refused` dans le JSONL (throttle 10 s,
chacune portant le compteur total). État final : `playing`.

**Mesure de contrôle** (bloc `trim_controller`, pour que le test ne soit pas vide) : un
contrôleur qui ne fait que des corrections de cadence voit passer **2399** commandes `rate` et
**zéro** refus. La garde n'est donc pas un refus en bloc : elle refuse les mouvements de tête de
lecture et laisse passer les trims, exactement §1.

**Conclusion** : l'acceptation est tenue **dans le code**, pas par convention. Le seul moyen de
recharger après le départ est une action de transport de l'utilisateur (pause→reprise, seek, ou
un `intent` venu du récepteur), qui ouvre une fenêtre d'atterrissage **à budget 2** (§1 : « un
seul re-atterrissage est permis ») refermée au tic suivant. C'est ce que demande la tâche.

**Ce que ça ne prouve pas** : les fakes n'ont pas de bruit ; `digital_error_ms = 0.0` et
`p95_ms = 0.0` sur cette session simulée ne disent **rien** de la boucle réelle (tâche F) ni de
la dispersion d'atterrissage mesurée en A25/A35. Aucun chiffre de synchronisation réel n'est
revendiqué ici.

## G3 — La commande de démarrage quand `sync.py` est encore un stub

**Hypothèse** : le service doit démarrer aujourd'hui, donc la session doit survivre à un
`Controller` qui lève `NotImplementedError` sur tout.

**Mesure** (test `test_a_stub_controller_still_starts`, et le log `controller_unavailable`
`fallback: stopgap_start`) : `session_started` quand même, un `load` (gare la vidéo) + `play`
groupe + `play_at`, et 20 tics plus tard toujours `playing`, sans exception.

**Conclusion** : le plan de secours `_stopgap_start` est **explicitement un stopgap** (loggé
comme tel, `task: F`) : il ne synchronise rien (± un `START_LEAD_S` de 0,2 s). Dès que la tâche
F renvoie un plan, il prend la place. Règle de passage : `Command.kind` appartenant à
`CONTROLLER_MESSAGES` → récepteur ; tout le reste → groupe, et `payload["target"] == "group"`
force le groupe (nécessaire pour `pause`, qui est ambigu).

## G4 — Crash et redémarrage : on ne défait que ce qu'on a fait (§8)

**Hypothèse** : `boot_cleanup()` doit arrêter la session qu'on a enregistrée et rien d'autre.

**Mesure** (bloc `boot_cleanup` du self-test) : un enregistrement `state/session.json`
(`group_prepared`, `receiver_launched`, `sid`, une ligne de mute par nous, une par
`cast-relay`) donne exactement `["group_stopped", "receiver_stopped", "proxy_closed",
"mute_undone:Cuisine"]` : `group.stop()` + `MSG_STOP` + `close_session(sid)` + **un seul**
`set_volume` (le mute du relais est ignoré), l'enregistrement est supprimé. Un enregistrement
`owner = cast-relay` : **zéro** action, fichier conservé.

**Conclusion** : `ExecStopPost` peut appeler `--cleanup` sans risque ; un service arrêté ne
casse ni la session d'un autre ni le volume d'autrui. **Corrigé en route** : `--cleanup` exécutait
le nettoyage deux fois (une passe au démarrage consommait l'enregistrement), donc la commande
d'`ExecStopPost` affichait `actions: []` après avoir pourtant tout arrêté. Il ne tourne plus
qu'une fois et rend la liste de ce qu'il a fait ; un test le vérifie sur la vraie CLI. Limite connue : `CastGroup` n'expose
aucun lecteur de volume, donc la session **ne mute rien** aujourd'hui (`mutes: []` dans
l'enregistrement) — la machinerie de restitution est écrite et testée, mais elle attend un
`volume_state()` côté tâche E (proposé en amendement).

## G5 — Heures calmes (§7)

**Mesure** : `refuse` → réponse `202 {"state": "quiet_refused"}` et **rien** n'est touché :
`group.calls == []`, `link.launched is False`, aucun sid ouvert, pas de verrou de groupe.
`video_only` → la vidéo démarre (`playing`) et `group.calls == []` (le groupe n'est **jamais**
chargé). Fenêtre ouverte → démarrage normal (`prepare` puis `play`). Une session **déjà** en
cours n'est pas arrêtée quand la fenêtre se ferme (le test pousse 4 tics avec une politique
fermée : toujours `playing`, aucun `stop`).

**Conclusion** : la règle porte bien sur le *démarrage*, et une fenêtre qui traverse minuit
(`22:00…06:00`) est gérée ; bornes testées à la minute près (`QUIET_END` exclu).

## G6 — Le verrou du groupe (§7)

**Mesure** : création par `O_EXCL` avec exactement les trois clés documentées
(`owner`, `pid`, `since`) ; un verrou étranger **frais** est respecté (`False`) ; il est repris
au-delà de **15 s** (mtime), avec une ligne `lock_takeover` qui porte `previous_owner`,
`previous_pid`, `age_s`, `reason` ; un verrou **frais du relais** est préempté (`reason:
owner_wins`, §7 « the v0.2 owner always wins ») ; le heartbeat rafraîchit le mtime et, si un
autre nous a pris le verrou, répond `False` + `lock_lost` au lieu de se battre ; `release()`
ne supprime jamais le fichier d'un autre (`lock_release_skipped`).

**Conclusion** : `state/group.owner` reste le point de rendez-vous avec le relais v0.1, sans
champ ajouté à l'enregistrement.

## G7 — Configuration (§6)

**Mesure** : `castsender.conf.example` se charge et rend **exactement** `CONFIG_DEFAULTS` ; les
11 clés sont les seules acceptées **dans un fichier** (une clé inconnue → `ConfigError`, donc
le service refuse de démarrer : un `DELTA=0.265` recopié de v0.1 ne peut pas passer inaperçu) ;
12 valeurs invalides sont refusées (APP_ID, port, DELTA_V2, RATE_MAX, MAX_HEIGHT,
QUIET_POLICY, moitié de fenêtre, hôte vide). Les variables d'environnement du processus, elles,
sont filtrées aux clés §6 (l'environnement contient tout le reste) — la voie stricte est le
fichier, qui est celle de systemd.

## G8 — Hors ligne par défaut, et le service démarre

**Mesure** : `python -m sender.service --offline --once` répond `200` sur `/api/status` et
écrit son JSONL. `SENDER_OFFLINE=1` fait la même chose sans argument. Un `HTTP_BIND` inexistant
(192.168.2.21 n'existe pas sur cette machine) n'est pas un traceback : c'est loggé, imprimé en
JSON et sorti en code 3. Le test de fumée monte le
service complet et vérifie que les seams sont bien `FakeReceiver`/`FakeGroup`/`FakeProxy` : en
mode hors ligne, **aucun paquet ne peut sortir** vers `.80` ni vers le groupe.

---

## §8. Reproductibilité

```sh
cd ~/opencode/nestaudiocast-wt/sender
./tests/v020/run.sh                                   # 124 tests, 0 échec
./tools/prove.sh --fast                               # suite de sécurité v0.1 : verte
.venv/bin/python -m sender.selftest \
    --evidence evidence/40-sender-selftest.json       # matrice + 10 min + boot cleanup
.venv/bin/python -m sender.service --offline --once    # le service démarre et répond
.venv/bin/python -m sender.service --offline --cleanup # ExecStopPost, sans enregistrement
```

## §9. Ce qui n'est pas prouvé, et pourquoi

1. **Toute preuve LAN.** Cette tâche est hors ligne par construction (consigne de la nuit) :
   aucun paquet n'est parti vers `.80`, le groupe ou la TV. Donc : pas de mesure de latence de
   lancement réelle, pas de `play_at` honoré, pas de volume réel.
2. **Les seams réels.** `lib/v020/{chromecast,group,media_proxy,sync}.py` sont des stubs
   (tâches B/D/E/F). Le service tourne, mais un partage en mode `--live` finit en
   `resolver_unavailable` / `sender_link_unavailable` — c'est testé et loggé, pas contourné.
3. **La synchronisation.** `digital_error_ms`/`p95_ms` sont calculés (médiane par fenêtre de
   5 s côté p95 = plus proche rang) mais sur des fakes sans bruit : aucun chiffre réel.
4. **Le mute.** Rien n'est muté aujourd'hui (pas de lecteur de volume dans `CastGroup`) ; la
   restitution est testée sur un enregistrement fabriqué, pas sur le matériel.
5. **`IPAddressAllow`** dans l'unité systemd suppose systemd ≥ 235 et un noyau avec
   cgroup/BPF : non vérifié sur CT 121 (rien n'est installé cette nuit).

## §10. Amendements proposés à `docs/v020-contracts.md` (je ne l'ai pas modifié)

1. **§4/§5 — `MediaProxy` n'a pas d'endpoint HTTP.** §5 exige la délégation de `/media/*`, mais
   le contrat ne donne que `mpd_for()` (qui rend une chaîne). Proposition : ajouter
   `MediaProxy.serve_http(method, path, headers) -> (status, headers, body)` ; en attendant,
   `/media/<sid>/<n>` répond `501 media_stream_unavailable` et le dit.
2. **§5 — où vit `assets/click_train_2310.m4a` ?** `measure: true` doit donner cette piste au
   groupe, et §5 n'a pas de route pour un asset. Proposition : `GET /assets/measure/<file>`
   (lecture seule, `.m4a`/`.wav`, pas de traversée). Implémenté tel quel.
3. **§6 — `CastGroup` n'a pas de lecteur de volume.** `set_volume(level, muted)` exige un
   niveau, donc un `{"action":"volume","muted":true}` seul est impossible à exprimer avant
   d'avoir commandé un niveau. Proposition : `CastGroup.volume_state() -> {"level","muted"}`.
   En attendant : l'API mémorise le dernier niveau qu'elle a commandé et répond `409
   no_known_level` si on lui demande un mute sans niveau connu.
4. **§5 — forme du champ `quiet`** dans `/api/status` (non spécifiée) : objet
   `{enabled, start, end, policy, allowed_now, decision}`.
5. **§5 — `steady_since`** : epoch (s) du début de l'état stable (dernier événement de
   transport + `STEADY_AFTER_S`), `null` sinon.
6. **§5 — code de refus silencieux.** `POST /api/share` répond `202` avec
   `state: "quiet_refused"` (même enveloppe que la réponse nominale) plutôt qu'un code
   d'erreur : la requête est acceptée, c'est la lecture qui est refusée. À confirmer par
   l'intégrateur.
7. **Convention de routage de `Command.kind`** (utile à la tâche F) : `CONTROLLER_MESSAGES` →
   récepteur ; tout autre `kind` → groupe (`play`, `pause`, `seek`, `stop`, `volume`,
   `prepare`) ; `payload["target"] == "group"` force le groupe (nécessaire pour `pause`).
   Un plan peut laisser `url` et `rx_ms` vides : la session les remplit (`rx_ms` appartient à
   la base du récepteur, seule la session a l'estimation d'horloge).
