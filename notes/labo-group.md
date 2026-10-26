# Cahier de laboratoire — tâche E : le groupe cuisine (v0.2.0)

Règle du projet : **hypothèse → mesure → conclusion → étape suivante**. Aucune affirmation sans
preuve exécutée ; « non prouvé, parce que X » est une réponse acceptable, un chiffre inventé est un
échec.

Branche `feat/group-sink`, worktree `~/opencode/nestaudiocast-wt/group`, contrat
`docs/v020-contracts.md` (gelé). **Règle de la nuit respectée : aucun paquet au groupe Cuisine** —
pas de LOAD, pas de PLAY, pas de SEEK, pas de volume. Tout ce qui suit est hors ligne (faux
appareils, horloge simulée), sauf la vérification **en lecture seule** de `tools/prove.sh --fast`
(mDNS + `MULTIZONE_STATUS`, qui ne jouent rien, A6).

---

## G1 — Ce qui vient de v0.1, et pourquoi rien n'est réécrit

**Hypothèse** : le comportement durement acquis de v0.1 tient dans quatre règles, et elles se
transposent telles quelles au groupe de v0.2 — inutile d'inventer une nouvelle boucle.

**Mesure** (lecture de `relay/cast_relay.py`, non importé, non modifié ; `notes/00-labo.md`) :

| Ce qui est repris | Où c'est mesuré | Ce que ça empêche |
|---|---|---|
| `groupguard.require_safe_group` à **chaque** connexion et reconnexion | A6, `evidence/09` | prendre la TV : les trois groupes du LAN contiennent un appareil vidéo |
| `CastLink.keepalive()` à chaque lecture | A23, `evidence/22` | un lien qu'on ne lit plus est fermé par le récepteur en ~24 s |
| garer en PAUSE puis un seul PLAY | A25/27, `evidence/27` (σ 30 ms contre 202 ms pour un LOAD) | recharger = trou audible de 1,6 s |
| **deux** lectures non-PLAYING consécutives avant de relancer | A36, `evidence/23-acoustic-v015b` | une seule lecture périmée a relancé à t=62 s et coupé une fenêtre acoustique en deux |
| médiane sur 5 + rejet d'un échantillon contredit par ses voisins | A15, `evidence/17` (σ 10,2 ms, pointes ±162/−156 ms) | une pointe isolée déclenche un SEEK inutile |
| muter, jamais un niveau à 0 | A37, `evidence/28` | le volume maître d'un groupe **multiplie** celui de ses membres : un membre à 0 reste muet à 0,4 |

**Conclusion** : `lib/v020/group.py` ne réimplémente aucune de ces règles, il les applique derrière
les signatures gelées. Deux ajouts seulement, tous deux exigés par le contrat : `restart_needed()`
(la décision de relance appartient au contrôleur, pas au sink) et `needs_reconnect()` (deux lectures
en échec, la politique d'A22).

**Suite** : éprouver hors ligne ce qui est éprouvable sans enceinte.

---

## G2 — L'estimateur de tête de lecture, hors ligne

**Hypothèse** : la médiane sur 5 + le rejet d'un échantillon contredit par ses voisins suffisent à
la fois (a) à ignorer une lecture périmée, (b) à accepter un vrai déplacement de 1,5 s, et (c) à
laisser la dérive mesurable sur 10 min sans être inventée sur une série plate.

**Mesure** — `.venv/bin/python` sur `v020.group.PlayheadEstimator` et `v020.group.drift()`, série
synthétique déterministe (graine 7, 4 Hz, 8 ms de bruit) ; sortie archivée dans
`evidence/v020-group-estimator-selftest.json` :

| Cas | Résultat | Ce que ça prouve |
|---|---|---|
| 240 lectures, 60 s, dérive 300 ppm (25× la dérive mesurée du groupe, A15 = −12,4 ms/10 min) | résidu **3,44 ms**, **0 rejet** sur 240 | le filtre ne confond pas le bruit normal avec une lecture périmée |
| une lecture périmée de **+156 ms** injectée | verdict `outlier`, estimateur déplacé de **0,0 ms** | une pointe isolée ne bouge pas la position estimée (A15) |
| deux lectures contradictoires de suite (+1,75 s puis +2,00 s) | `outlier` puis **`jump`** | deux lectures d'accord entre elles et pas avec la fenêtre = un vrai déplacement, pas deux pannes : la fenêtre redémarre au lieu de rejeter indéfiniment |
| 10 min, dérive 300 ppm, médianes de 5 s (contrat §2) | **+180,4 ms/10 min ± 1,8**, `significant = true` | la machinerie du sondeur retrouve la dérive réelle (vraie valeur 180) |
| 10 min, série **plate** | **−1,4 ms/10 min ± 1,8**, `significant = false` | le test à 2σ n'invente pas de tendance |
| pause | position **figée**, jamais extrapolée | une tête de lecture en pause n'avance pas : l'extrapoler fabrique une erreur qu'aucune boucle ne peut voir |
| reprise après pause | nouvelle fenêtre, **0 jump** | deux chronologies ne se mélangent pas dans une même médiane |

Le taux n'est **pas** déduit d'une fenêtre de 5 points (elle annoncerait 1,013 pour un vrai 1,0003 :
c'est du bruit). La pente instantanée est donc bornée à ±2 % de 1,0, et le taux réel se mesure sur
la série longue : **1,000334 ± 0,000030**, cohérent avec `evidence/17`.

**Conclusion** : les trois propriétés demandées (résidu rapporté, rejet d'une lecture périmée,
hystérésis à deux lectures) sont **prouvées hors ligne**. Le résidu mesuré (3,4 ms) est celui d'une
fenêtre de 5 points ; sur le vrai groupe, A15 donne σ 10,2 ms — le sondeur mesurera le vrai chiffre,
il n'est pas supposé ici.

**Suite** : la décision de relance et la sécurité, également hors ligne.

---

## G3 — Sécurité de session et d'hystérésis, hors ligne

**Mesure** — `tests/v020/test_group.py` (61 tests, `evidence/v020-group-offline-tests.log`), avec
`tests/v020/fakes.FakeGroup` et un **faux appareil CASTv2** (`FakeDevice`, scripté en
`MEDIA_STATUS`), horloge simulée, aucun socket :

- **A6** : garde exécutée **avant** l'ouverture de notre lien — un groupe non sûr lève
  `UnsafeGroup` et la liste des liens construits reste vide (test
  `test_an_unsafe_group_is_refused_before_any_link_is_opened`).
- **mDNS à chaque démarrage** : le port d'un groupe Cast est dynamique (A19) ; un second `connect()`
  avec un port différent est gardé et utilisé (`test_a_new_port_is_used_at_the_next_start`).
- **reconnexion** : la garde tourne de nouveau, l'ancien lien est fermé même quand la garde refuse ;
  une session survécue n'est **adoptée** que si son `mediaSessionId` est celui qu'on a créé.
- **`stop()`** : ne touche **jamais** une session étrangère (`stopped == []`), et ne fait rien quand
  cet objet n'a rien créé. Une session étrangère détectée pendant une lecture efface notre
  propriété et fait lever `ForeignSession` aux commandes suivantes.
- **pas de rechargement** : `prepare` → `play` donne exactement `1 LOAD, 1 PLAY`, et aucun SEEK
  après le PLAY ; un second `prepare` sur le même contenu utilise PAUSE+SEEK.
- **A36** : une lecture non-PLAYING ne suffit pas, deux oui ; aucune lecture ne compte pendant la
  fenêtre de stabilisation de 2 s après un PLAY (A21) ; un groupe garé n'est jamais « relancé ».
- **A37** : `set_volume(0.0)` n'envoie **aucun niveau 0** — il envoie `muted=true` sans toucher le
  niveau ; le miroir n'agit que sur front ; un mute est idempotent.

**Conclusion** : les propriétés de sécurité de la tâche E sont tenues par construction et par test,
pas par convention.

---

## G4 — `tools/group_probe_v020.py` : écrit, **non exécuté**

**Hypothèse** : la mesure que la porte matérielle devra lancer (dispersion de l'actionneur
PAUSE→SEEK→PLAY, puis résidu de l'estimateur et dérive réelle sur 10 min) peut être écrite et
**prouvée inerte** aujourd'hui, sans envoyer un seul paquet.

**Mesure** — `evidence/v020-group-probe-dryrun.log` : mode par défaut = plan, `exit 0`,
`transport_constructed: 0`, `castv2_imported: false` (aucun mDNS, aucun socket, aucun objet de
transport). Le compteur est instrumenté dans le script : une exécution à blanc qui construirait un
transport le dirait et sortirait en code 3. Refus vérifiés et testés (code 2) : sans `--mute`, sans
`--i-have-an-audio-window`, sans `--url` ; refus par `groupguard` en code 4 (aucun paquet avant).

Le plan imprimé est exactement le protocole exécuté : 8 atterrissages
(PAUSE→SEEK→PLAY, pas de 20 s), puis **une** dernière mise en pause suivie d'un PLAY, puis
`--minutes` (10 par défaut) de lectures à 4 Hz **sans aucun événement de transport** (c'est ce que
le contrat §2 appelle régime établi), puis arrêt de la session créée et restauration
niveau/mute.

**Conclusion** : la sonde existe, elle est inerte par défaut, elle refuse tout ce qu'elle doit
refuser. **Non prouvé : ses chiffres.** Aucune dispersion d'atterrissage ni aucune dérive réelle n'a
été mesurée ce soir — c'est précisément la fenêtre audio que le propriétaire n'a pas ouverte.

**Suite** : le jour où la fenêtre s'ouvre,
`.venv/bin/python tools/group_probe_v020.py --run --mute --i-have-an-audio-window --url <audio.m4a>`
écrit `evidence/group-probe-<horodatage>.json`.

---

## G5 — Les deux suites du projet, et ce qu'elles prouvent ce soir

**Mesure** :

| Commande | Résultat | Fichier |
|---|---|---|
| `./tests/v020/run.sh` | **61 tests, OK** (0,2 s) | `evidence/v020-group-offline-tests.log` |
| `./tools/prove.sh --fast` | sortie **0** : analyseur PASS, cuisine **silencieuse** (`audible right now: none`), groupe **Cuisine SAFE** (5 Nest Audio, aucun appareil vidéo), 4 gardes de régression OK | `evidence/v020-group-prove-fast.log` |
| `tools/group_probe_v020.py --dry-run` | sortie **0**, `transport_constructed: 0` | `evidence/v020-group-probe-dryrun.log` |
| estimateur synthétique | résidu **3,44 ms**, 0 rejet / 240 ; périmée +156 ms rejetée ; dérive 10 min **180,4 ± 1,8 ms/10 min** sur 300 ppm, **−1,4 ± 1,8** sur une série plate | `evidence/v020-group-estimator-selftest.json` |

`prove.sh --fast` est la seule commande de la liste qui parle au LAN, et elle ne fait que lire :
mDNS puis `MULTIZONE_STATUS`. Elle confirme au passage que le groupe `Cuisine` que la sonde
résoudra par nom est **le bon** et qu'il est acceptable pour la garde.

---

## G6 — Ce qui n'est **pas** prouvé, et les limites assumées

1. **Aucun paquet au groupe, donc aucune mesure matérielle.** La dispersion d'atterrissage
   PAUSE→SEEK→PLAY de v0.2, le résidu réel de l'estimateur sur 10 min et la dérive réelle du groupe
   en v0.2 sont **non mesurés**. Les chiffres cités (σ 30 ms, σ 10,2 ms, −12,4 ms/10 min) sont ceux
   de v0.1, `evidence/17` et `evidence/27` — ils motivent le code, ils ne le valident pas.
2. **La sonde n'a jamais tourné.** Son plan est prouvé inerte ; son protocole n'est prouvé que par
   lecture et par les tests de ses refus.
3. **`sleep`/`now` sont des attributs injectables.** C'est ce qui rend la suite hors ligne possible ;
   en production ce sont `time.sleep`/`time.time`, et le déroulement réel (latence de `media_status`
   sous charge) n'est pas mesuré.
4. **Le pare-feu de sécurité de l'ordre des appels** (garde **avant** lien) est prouvé par un faux :
   sur le vrai groupe, `require_safe_group` ouvre son propre lien de lecture (`MULTIZONE_STATUS`),
   donc « aucun lien » veut dire « aucun lien **à nous** ». C'est la lecture correcte de la règle,
   et c'est dit ici pour que personne ne la surinterprète.
5. **Une fenêtre de 5 points ne mesure pas un taux.** Elle estime un niveau ; le taux réel vient de
   la série longue (pente bornée à ±2 % sur la fenêtre courte). Écrire l'inverse serait un chiffre
   inventé.
6. **Le dé-mutage au démarrage n'est pas dans `CastGroup`.** v0.1 le fait dans
   `GroupSink.connect()` parce qu'il mute lui-même en s'arrêtant, membre par membre. Le sink de
   v0.2 ne mute personne en s'arrêtant (le rollback et le silence sont `lib/emergency.py` et
   `lib/silence.py`) : démuter à l'aveugle à la connexion annulerait un mute **du propriétaire**, et
   un dé-mutage de groupe ne lève d'ailleurs pas le flag de chaque membre. La règle « annule au
   démarrage le mute que *tu* as posé » suppose donc un enregistrement de ce mute ; c'est le
   nettoyage de démarrage du service (contrat §8), pas la tâche E. Ce que la tâche E fait : **lire**
   les niveaux à la connexion et **avertir** `WARNING group members are silent` quand tous les
   membres sont muets (le bug A37 qui rend la cuisine muette alors qu'elle joue).
7. **`--minutes` très court** (disons < 2 min) donne un nombre de fenêtres trop faible pour un test
   à 2σ : la sonde le rapporte (`n`, `significant`) mais ne refuse pas la valeur — c'est au lecteur
   de ne pas conclure. Le garde-fou serait un refus explicite ; non implémenté, non demandé.

---

## G7 — Contradictions avec `docs/v020-contracts.md`

Aucune contradiction de fond. Trois précisions proposées à l'intégrateur (le contrat n'est **pas**
modifié ici) :

1. **`__init__` de `CastGroup`** : le contrat fige les *méthodes* (`connect`, `prepare`, …) et le
   test `test_contracts` ne vérifie que celles-là. La signature du constructeur est conservée
   **telle quelle** (`name, host, port, log`) ; les points d'injection (`now`, `sleep`, `_discover`,
   `_guard`, `_open_link`) sont des *attributs/méthodes protégées*, pas des paramètres. Rien à
   amender, mais §1 gagnerait une ligne : « les tests injectent l'horloge et le transport par les
   attributs `now`, `sleep` et les méthodes `_discover`/`_guard`/`_open_link` ».
2. **`sample()` peut renvoyer `None`** (lien mort, aucune session, ou lecture rejetée par
   l'estimateur). Le contrat écrit `GroupSample | None` sans dire *quand* : proposé —
   « `None` = ne rien faire de cette lecture ; c'est le rejet d'une lecture périmée (A15), pas une
   erreur ».
3. **`restart_needed()` et `needs_reconnect()`** ne sont pas dans le contrat mais sont nécessaires
   au contrôleur (tâche F) : la décision de relance et la politique de reconnexion d'A22 ne peuvent
   pas vivre dans le sink. Proposé d'ajouter ces deux noms à §1.

Détail d'outillage, hors contrat : `tests/v020/run.sh <module>` ne fonctionne pas (il passe `-s`/`-p`
à `python -m unittest discover`, qui les refuse). Le lancer sans argument ; ou corriger la ligne 9
(`exec $PY -m unittest -v "discover" -s tests/v020 -p "$1.py"` → `exec $PY -m unittest -v
"tests.v020.$1"`).
« Je n'ai pas touché au fichier : il appartient à l'intégrateur. »
