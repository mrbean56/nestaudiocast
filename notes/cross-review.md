# Relecture croisée — PoC « relais Cast cuisine/TV »

Relecteur indépendant. Rôle : attaquer les preuves, pas valider le travail.
Aucune commande réseau émise, aucune lecture audio, rien envoyé à 192.168.2.80.
Tout ce qui suit est vérifié par lecture de fichiers, calcul sur les JSON bruts, ou
ré-exécution locale de `measure/selftest_analyzer.py` et `relay/sim.py` **dans une copie**
(`~/.cache/nac-review`), pour ne pas écraser `evidence/`.

Versions auditées (mtimes) : `00-labo.md` 01:17:51 · `track1-protocol.md` 01:05:37 ·
`track3-hardware.md` 00:35:10 · `measurement-protocol.md` 01:16:43 · `rollback.md` 01:17:39 ·
`ct121-readme.md` 01:20:50. Les notes ont bougé pendant la relecture (A7–A10,
measurement-protocol, rollback, ct121 sont apparus en cours de route).

---

## 0. Le point qui commande tout le reste

**Aucune mesure d'offset A/V n'a jamais été faite sur le LAN.** Pas une seule ligne des
notes ne prétend le contraire (c'est à leur honneur : A6 finit par « attaquer la mesure
acoustique de l'offset réel »), mais cela signifie que le critère d'acceptation du PoC
(≤ 150 ms perçu) n'a **aucune preuve**, et que tout ce qui est présenté comme validé
(« la loi de commande tient », « l'instrument peut être utilisé en confiance ») est validé
sur des données synthétiques, pas sur la maison.

Corollaire dur : **le groupe cuisine-only n'existe pas encore**. Donc le trajet complet
`TV → relais → groupe → oreille` n'a jamais tourné une seule fois. Le service livré
(`ct121-readme.md` L24) est désactivé tant que `GROUP` est vide : l'objet décrit ne peut
pas démarrer.

---

## 1. Audit affirmation par affirmation

| # | Affirmation (citation exacte) | Source citée | Verdict | Ce que j'ai vérifié |
|---|---|---|---|---|
| A1 | « mDNS → **15 cibles** » | 01, 05 | **FAUX (16)** | `01-discover-cast.txt` L1 : « 16 Cast device(s) seen », 16 blocs. 05.json ne contient que 6 entrées, pas 15 : la citation de 05 pour le décompte est fausse. |
| A1 | « TV = « Chromecast Salon » .80 (Chromecast 3e gén.) » | 01 | **NON ÉTAYÉ** pour la génération | 01 ne donne que `md: Chromecast`, `caps=201221` — identique à « Basement TV ». La génération ne se déduit d'aucune preuve. |
| A2 | « `GET_STATUS` … renvoie `contentId="VyNgbQKORiA"`, `currentTime=1595.501`, `playerState=PLAYING`, `mediaSessionId=2147215415`, et des poussées `MEDIA_STATUS` régulières » (00-labo L28-32, source `evidence/04`) | **04** | **MÉSUSAGE DE SOURCE + 3 valeurs introuvables** | `04-raw-castv2-probe.json` L44 : `"media_error": "timeout waiting for urn:x-cast:com.google.cast.media reply to GET_STATUS"` — **aucun** des champs cités n'y est. `grep` sur tout `evidence/` : `1595.501` et `2147215415` n'existent nulle part. La seule lecture TV réussie est `05-device-snapshot.log` L6 : `"state": "PLAYING", "t": 1675.659, "content": "VyNgbQKORiA"` — **un** échantillon. |
| A2 | « on lit l'identifiant vidéo et la tête de lecture de la session TV **en temps réel** » (L34-35) | 04, 05 | **SURESTIMÉ** | Une seule lecture réussie dans toutes les preuves, aucune série. Le journal du relais réel, `evidence/relay-20261025-011350.jsonl`, fait **0 octet**. « en temps réel »/« poussées régulières » = non mesuré. |
| A3 | « `LAUNCH` DMR → OK en **1362 ms** ; `LOAD` … `PLAYING`, `currentTime` progresse 0 → **14.988 s** ; les 5 Nest Audio exécutent le récepteur » | 06 | **SOLIDE** | 06 : `launch_dmr_ok.ms=1362`, `group_samples[-1].currentTime=14.988044`, `membership_after_load` = 5×`Default Media Receiver`. |
| A3 | « on peut jouer notre propre audio sur le groupe » | 06 | **SURESTIMÉ (protocole oui, audio non)** | Tout tournait muet par construction. Aucun test n'a jamais confirmé qu'un son sort d'une enceinte. Le PoC n'a jamais produit un seul son. |
| A4 | « première mesure `PLAYING` à `t=60.000` **122 ms** après le `LOAD` » | 07 | **CHIFFRE JUSTE, INFÉRENCE FAUSSE** | `07-seek-precision.json` `load_trial.samples[0]` : `wall−load_wall=121.86 ms`, `t=60` — mais ce même échantillon (`rtt_ms=192`) est suivi de `t=60.09 state=BUFFERING` et `t=60.37 BUFFERING`. L'appareil annonce donc `PLAYING` **avant** de bufferiser : cet échantillon ne date pas le début du son. |
| A4 | « décalage constant **−0.37 s** (démarrage réel du flux ~0.37 s après le `LOAD`), résidu ~10 ms » | 06 | **REPRODUCTIBLE, MAIS MAL ATTRIBUÉ** | Recalculé : `t_i −(w_i−w_0)` médiane **−381.5 ms**, écart-type **7.9 ms** (échantillons 3..30) — donc −0.37 s et ~10 ms sont bien là. **Mais** l'origine est le **premier échantillon**, situé **268 ms après le LOAD** (`w_0−t_load=0.268`). Le début réel du flux est donc à **0.65 s après le LOAD** (0.636–0.650 selon l'ajustement). La phrase fait deux erreurs : elle attribue à `LOAD` un zéro mesuré 268 ms plus tard, et elle ignore que les 2 premiers échantillons sont aberrants (taux implicite **1.63×** entre l'échantillon 2 et 3 — physiquement impossible : réponse périmée). |
| A4 | « Cadence sur 20 s : **1.000091** → **+55 ms par 10 min** » | 07 | **CHIFFRE EXACT / CONCLUSION NON SOUTENUE** | Recalcul sur `drift_trial` (40 pts, 20.00 s) : pente **1.00009076** → **+54.5 ms/10 min** ✓. Mais erreur-type sur la pente = **1.91e-4** → **±114 ms/10 min à 1σ** (résidu 7.0 ms). Les +55 ms/10 min valent **0.48σ** : indistinguable de zéro. « dérive de l'ordre de 55 ms/10 min, sous le budget » (L63) présente du bruit comme une mesure. |
| A4/A8 | « démarrage déterministe (~0.4 s) », sim `START_LATENCY=0.40` | 06,07, **16** | **FAUX (~0.55–0.65 s)** | Trois estimations indépendantes : 06 → 0.65 s ; 07 (extrapolation des 3 points `BUFFERING`→`PLAYING` du `load_trial`) → **0.59 s** ; `16-single-device-latency.json` → `time_to_playing_s` **0.542 (wav) / 0.612 (m4a)**. Le simulateur sous-estime donc le démarrage de ~0.2 s, et `SEEK_LEAD_S=0.50` (`cast_relay.py` L52) de ~0.05–0.15 s. Conséquence : chaque démarrage de session arrive ~50–150 ms en retard, corrigé à 2 % de cadence pendant 4–8 s (ou par un SEEK si l'écart dépasse 250 ms). |
| A5 | 8 SEEK, « erreur **−410 à −597 ms** … dispersion **±90 ms** » | 08 | **SOLIDE, RECALCULÉ** | 08 `seek[*].err_ms` : min −597.4, max −409.8, étendue 187.6 → **±93.8**, σ=73.7, n=8. Latence implicite 410–597 ms (moy. 507, σ 74) ; 07 donne 0.36–0.55 s. Cohérent. |
| A5 | « Commandes annoncées par le groupe : `PAUSE, SEEK, STREAM_VOLUME, STREAM_MUTE, PLAYBACK_RATE, LIKE, VOLUME_STEP` » | 07/08 `supportedMediaCommands=274447` | **FAUX** | 274447 = PAUSE│SEEK│STREAM_VOLUME│STREAM_MUTE│**LIKE│DISLIKE│QUEUE_REORDER**. **`PLAYBACK_RATE` (2048) n'est PAS annoncé**, `GET_STATUS` (256) non plus, et `VOLUME_STEP` n'est pas une commande Cast. |
| A5 | « `SET_PLAYBACK_RATE` … **honoré** : ratio média/mur **1.004132** à 1.005 demandé » | 08 | **DIRECTION OK, GAIN NON MESURÉ** | Le ratio est calculé sur **deux points** (premier/dernier) espacés de ~4.4 s (12×0.4 s). Bruit d'une lecture de `currentTime` ≈ 7–10 ms → incertitude relative **±0.0023**. Donc 1.004132 = +0.0041 ± 0.0023 : le « gain » réel se situe entre 0.4 et 1.8, et même le fait que la cadence change n'est qu'à ~1.8σ. Sur un appareil qui **n'annonce pas** `PLAYBACK_RATE`, c'est mince. |
| A5 | « `PLAYBACK_RATE` (**fin, continu, sans trou**) » ; code : « fine, continuous, **inaudible** » (`cast_relay.py` L18-19) | 08 | **SURESTIMÉ** | La boucle clampe à **±2 %** (`MAX_RATE_DELTA=0.02`) ⇒ **±34 cents** de décalage de hauteur, pendant plusieurs secondes à chaque correction. Sur de la musique, c'est audible. « Sans trou » ✓ ; « inaudible » ✗. |
| A6 | tableau des 3 groupes, « aucun groupe existant n'est réservé à la cuisine » | 09, 10 | **SOLIDE** | 09.json : Groupe Tous = 5 Nest (caps 196612) + .81 + .80 (caps 196613) ; En Haut idem sans .81 ; Dehors = .81 + .80. 10 identique. |
| A6 | « c'est **le mécanisme exact** d'une interruption de la TV » | 09, 08 | **CORRÉLATION, PAS MÉCANISME** | 08 `groups["*"].detail["Chromecast Salon (TV)"].app = "Default Media Receiver"` : la TV exécutait bien le DMR du groupe. C'est fort, mais il manque la séquence avant/après sur .80 (aucune trace de la session YouTube interrompue, `05.json` montre « Backdrop » sans horodatage). |
| A6 | « les tests A3/A4/A5 » (périmètre de l'incident) | 06–08 | **SOUS-ESTIMÉ** | `probe/groups_seek_rate.py` L67-100 : `for gname,(host,port) in GROUPS.items()` puis `grp.launch(DMR)` (L72) + `LOAD` ⇒ **les trois groupes**, chacun avec la TV. 08 L2-4 (`members = []` ×3) en garde la trace, plus 06 et 07. `rollback.md` parle de « deux tests » : c'est au moins quatre prises de contrôle. |
| A7 | « `sender_disconnected` = 0 (l'expéditeur ne ferme pas proprement, il abandonne) » | 11 | **FAUX (artefact de journal)** | `probe/virtual_receiver.py` L184-186 : `if not data: break` — une fermeture **propre** (FIN) sort par `break`, n'entre pas dans le `except`, donc **ne journalise rien**. 0 ne distingue pas « abandon » de « fermeture propre ». |
| A7 | « `CONNECT` = 0 … aucune tentative de `LAUNCH` » | 11 | **SOLIDE** | 11 : zéro ligne `recv` (le `log("recv",…)` est appelé pour tout message hors deviceauth, L264-265). L'expéditeur n'a donc envoyé **que** le challenge d'auth. |
| A7 | « 0 sur **~15 tentatives** », toutes attribuées à `192.168.2.2` | 11 | **INEXACT + pair non documenté** | 11 : **12** connexions TLS depuis `.2` (6 cycles de nonce) et **14** depuis **`192.168.2.115`** (le portable du propriétaire, absent de toutes les notes). ~15 n'est vrai qu'en mélangeant les deux hôtes. |
| A7 | « MAC ASUSTek `bc:fc:e7:4f:f3:63`, sert `llama.cpp` sur :8080 » | — | **NON ÉTAYÉ** | Ces trois faits n'apparaissent **dans aucun fichier de `evidence/`** (grep). Plausible, mais hors dossier de preuve. |
| A8 | tableau S1/S2/S3 (44.5 / 47.4 / 22.6 ms, 100 % dans ±50 ms) | 13 | **CHIFFRES EXACTS, STATISTIQUE FILTRÉE** | J'ai ré-exécuté `relay/sim.py` dans une copie : S1 mean **−6.9** (note −7.0), max **44.5** (identique), S2 24.7/47.4, S3 1.8/22.6 — reproductible à ≤0.1 ms. **Mais** `sim.analyze()` (L187-200) **exclut** les échantillons `action=="seek"` et les 3 s après chaque événement. Sans filtre, S1 contient `err_ms = **−585000.4**` (le seek utilisateur vers 600 s, groupe encore à 15 s) et le taux « dans ±50 ms » tombe à **99.2 %**. « Erreur max 44.5 ms » n'est donc vrai qu'hors événements. |
| A8 | « la loi de commande tient l'erreur sous 50 ms dans les trois cas » ; « absorbe une dérive d'horloge » | 13 | **S1 NE TESTE PAS LA DÉRIVE** | 91 ppm × 72 s = **6.6 ms** de dérive cumulée, soit **< le seuil mort `FINE_S=0.03`** : S1 « tient » sans rien corriger. La dérive n'est réellement exercée que par S2 (3000 ppm = 55× la valeur mesurée). Aucun scénario ne dure 600 s. |
| A8 | « le paramètre `delta` est suivi à **±2 ms près** (ce qui prouve que la compensation acoustique sera applicable) » | 13 | **FAUX ET INVERSÉ** | S3 : moyenne 1.8 ms mais **max 22.6 ms**. Surtout, S3 prouve le contraire de ce qui est affirmé : la boucle maintient **joyeusement** un `delta` arbitraire de 200 ms sans le détecter. Elle ne peut donc pas valider `delta` ; c'est la mesure acoustique seule qui le fait. |
| A9 | tableau (0/36/120/−86/64 ms ; dérive 8.1 ms/min) | 14 | **EXACT, RE-EXÉCUTÉ** | `selftest_analyzer.py` relancé : 0.0 / 36.0 / 120.0 / −86.0 / 64.0, dérive 8.1 — conforme à A9. |
| A9 | « l'instrument peut être utilisé **en confiance** … ±4 ms » | 14 | **SURESTIMÉ** | 5 cas synthétiques, **une seule graine**, offsets seulement dans [−85, +120] ms, « lit musical » à **90 Hz** donc hors des deux bandes de mesure. Voir §2.3 : l'instrument **échoue** hors de ce domaine, dont un faux PASS. |
| A10 | « `lib/emergency.py` → … puis **restauration exacte des volumes, confirmée valeur par valeur** par `lib/silence.py` » | 15 | **NON ÉTAYÉ** | `15-panic-test.log` ne contient que des chaînes `"muted"` / `"idle"` : **aucune valeur de volume**, aucun avant/après, aucune trace de `--restore`. Il ne peut pas « confirmer valeur par valeur ». |
| T1 | « un récepteur Cast fait maison est réalisable et se comporte correctement … **8/8 étapes** » (L24-25) | 12 | **BOUCLE LOCALE** | 12 : `"host": "127.0.0.1"`, produit par `test_virtual_receiver.py`. C'est notre client contre notre serveur : cela valide notre sérialisation, pas le protocole face à un expéditeur réel. Le « sur le LAN » (L13) vaut pour l'annonce mDNS (11 le prouve), pas pour le protocole. |
| T1 | « ratio média/mur mesuré **1.0002** » (L22) | 12 | **CIRCULAIRE** | La tête de lecture est calculée par `Session.current_time()` du récepteur virtuel lui-même. Sur 2.02 s, 1.0002 = 0.4 ms : ne mesure rien. |
| T1 | « les deux sont également fatales, et la seconde est improbable vu que la réponse est cryptographiquement valide et bien formée » (L64-67) | 11 | **ARGUMENT FAIBLE** | « valide et bien formée » = vérifié **par notre propre parseur** (self-check). Un parseur Google n'est pas notre parseur. Voir §3.1 : au moins 4 causes concurrentes, dont une lacune de protocole probable. |
| T3 | BOM « 129,06 $ » ; « ~110 $ » ; « ~55 $ » | — | **ARITHMÉTIQUE OK** | 25.99+21.95+22+31.49+27.63 = 129.06 ; −31.49+13 = 110.57 ; 25.99+27.63 = 53.62. |
| T3 | « Aucun matériel n'est présent sur le LAN, donc aucune valeur n'est mesurée » | — | **HONNÊTE** | Les sources externes (support.google, WiiM, B&H) ne sont pas vérifiées par moi — hors de ma portée ici, et la note les marque `[documented]`/`[anecdote]` correctement. |
| M-P | « le générateur existe : `tools/make_assets.py --click-train 2000` » | — | **FAUX À L'ÉCRITURE, CORRIGÉ DEPUIS (01:21)** | Au moment de la note (01:16), `make_assets.py` n'acceptait que 2 arguments positionnels et ne générait que **1 kHz**. Depuis 01:21 l'option existe et les assets sont là : `click_train_2k.wav` (720 s, mono 48 k, premier clic à t=0.0026 s, pic spectral ~2 kHz) et `.m4a` (12 min, AAC 11 kbps). Reste un piège : `phase` vaut 0 par défaut, donc la phase du clic doit être **alignée à la main** sur celle du bip de la vidéo de test ; avec une période de 1 s, une erreur de phase est indiscernable d'un offset (§2.4). |
| M-P | « `delta = L_tv − L_grp` » (§0, L26) | — | **SIGNE INVERSÉ, contredit §4** | Voir §4.1. Le code est correct par calibration empirique ; la doc se contredit elle-même. |
| M-P | « vérifier **une fois** que la latence du groupe ne dépend pas du contenu (comparer l'offset numérique avant/après substitution) » (§6, L100-102) | — | **TEST NUL** | L'offset numérique ne voit pas la latence de rendu — c'est écrit deux paragraphes plus haut (§0). Comparer l'offset numérique avant/après ne peut donc **pas** détecter un changement de latence de rendu. Le garde-fou ne teste rien. |
| RB | « Testé le 2026-10-25 **01:38** » + « confirmée valeur par valeur » | 15 | **INCOHÉRENT** | `15-panic-test.log` est daté **01:17:03**, `emergency.py` **01:16**. 01:38 est postérieur à l'artefact (et postérieur à l'heure d'écriture de la note). Aucune preuve de la restauration. |
| RB | « n'envoie **jamais** de commande à la TV » | — | **VRAI PAR ADRESSE, FAUX PAR EFFET** | `emergency.py` L53-67 fait `SET_VOLUME(0, muted)` sur les **3 groupes**, dont deux contiennent .80 : le volume de la TV peut changer. La garantie est « pas de paquet vers .80 », pas « la TV n'est pas affectée ». |

---

## 2. Trous méthodologiques

### 2.1 « L'expéditeur Google abandonne → un récepteur DIY est impossible »

Le verdict (« réfutée pour l'app YouTube ») est très probablement **juste** : un certificat
d'appareil signé Google n'est pas forgeable, et c'est un fait connu du protocole. Mais la
**mesure présentée ne le prouve pas**, et la note le reconnaît à moitié.

Ce que la mesure établit : l'expéditeur ouvre TLS, envoie **un** challenge deviceauth, reçoit
notre réponse, et **n'envoie plus jamais rien** (0 ligne `recv`). C'est tout.

Causes concurrentes, toutes compatibles avec ce silence :

1. **Chaîne non signée par Google** (hypothèse de la note) — la plus probable.
2. **Réponse malformée pour le parseur Google.** Le self-check (round-trip par notre propre
   protobuf, signature vérifiée par notre propre clé) ne dit **rien** sur les attentes d'un
   tiers. Pistes concrètes dans `virtual_receiver.py` L317-324 :
   - `intermediate_certificate` vide et `crl` vide — légitime pour un auto-signé, mais
     l'émetteur peut exiger une chaîne ;
   - `hash_algorithm` de la réponse **codé en dur SHA256** alors que le challenge annonce
     `signature_algorithm=1` et **n'annonce pas** son `hash_algorithm` (non journalisé) :
     signer en SHA256 un challenge demandé en SHA1 échouerait même avec un certificat valide ;
   - la signature ne couvre que `sender_nonce` ; si le pair attend une signature sur
     `sender_nonce‖receiver_nonce` ou sur un liant TLS, elle est invalide.
3. **Poignée de main incomplète.** Le récepteur ne renvoie **jamais** de challenge vers
   l'expéditeur : `if which != "challenge": return` (L306-307) jette la réponse de
   l'expéditeur sans la traiter. Si le protocole est mutuel, l'expéditeur attend un challenge
   qui n'arrive jamais et abandonne — sans que ce soit le certificat. C'est le trou le plus
   sérieux et le moins coûteux à fermer.
4. **Le pair n'est peut-être pas un expéditeur Cast complet.** `.2` n'est identifié par aucune
   preuve (`MAC`, `llama.cpp` : introuvables dans `evidence/`). Et `.115` échoue à l'identique
   sans être mentionné.

Test décisif, bon marché, non fait : `chrome://media-router-internals` sur `.115` (Chrome est
là, c'est lui qui apparaît dans 11) donne le **motif d'échec en clair** (cert vs parse vs
timeout). Idem `logcat` côté Android. Tant que ce n'est pas fait, la note ne peut pas écrire
que la seconde hypothèse est « improbable ».

Second défaut de raisonnement : le verdict est **sous-argumenté**. Même avec un certificat
valide, un récepteur DIY devrait encore **obtenir le binaire du récepteur YouTube**
(`233637DE`) auprès de Google : ce n'est pas distribué publiquement. Le certificat est
nécessaire, pas suffisant. La conclusion tient, mais pour une raison plus large que celle
écrite.

Troisième : la répétition (~15 fois, mêmes nonces) ne discrimine rien. Une réponse malformée
échoue aussi de façon déterministe.

Enfin, la réserve honnête de L58-59 (« à confirmer visuellement demain ») reste ouverte : on
ne sait toujours pas si « Relais Cast PoC » apparaît dans la liste du téléphone. `.2` l'a
découvert — c'est un indice, pas la confirmation demandée.

### 2.2 Le socle « observateur » n'est pas fiable

C'est le point le plus grave pour la suite, et il est absent des notes.

- `04` (00:32) : `GET_STATUS` média sur YouTube → **timeout 8 s**.
- `05.json` (00:37) : TV sur « Backdrop » → timeout (attendu, mais l'app est morte).
- `05.log` (00:34) : TV sur YouTube → **succès** — une seule lecture.
- `relay-20261025-011350.jsonl` : **0 octet**. Le seul essai réel du relais n'a rien produit.

La différence de code entre `04` et `05` est `time.sleep(0.2)` après `connect_transport`
(`snapshot_all.py` L32) ; `RemoteTv.snapshot()` n'attend que **0.10 s** et avec
`media_status(timeout=2)` (`cast_relay.py` L84, L94). Le chemin dont dépend **tout** le PoC
n'a jamais été démontré à 4 Hz pendant 10 minutes. Pire : en cas de timeout,
`RemoteTv.close()` est appelé (L107-109) et la boucle **rouvre une connexion TLS vers .80
toutes les 250 ms**. Sur un appareil qui limite les connexions (le commentaire de
`castv2.py` L66-68 le dit), cela veut dire marteler la TV — contraire à l'esprit du
« lecture seule » et non discuté dans les notes.

Autre signe d'instabilité de la source de vérité : dans `07.load_trial`, l'appareil passe
`PLAYING (t=60.000)` → `BUFFERING (t=60.09)` → `PLAYING`. La machine à états rapportée
**recule**. Le relais prend ces états au pied de la lettre (`cast_relay.py` L376-379 :
`if g.state != PLAYING: _start_sink(...)`), ce qui, sur ce comportement réel, relancerait un
`LOAD` complet pendant un simple rebuffer.

### 2.3 Le protocole acoustique mesure-t-il l'offset *perçu* ?

Il mesure **un** écart audio-audio à **un** point. Ce n'est pas la même chose que l'écart
perçu image/son, et il manque quatre choses :

1. **La référence est le son de la TV, pas ses pixels.** Tout le protocole repose sur
   « le bip TV est solidaire de l'image » (`measurement-protocol.md` L45-47). C'est une
   **hypothèse non mesurée** sur la synchro interne de la TV — et c'est précisément
   l'hypothèse que l'option A de `track3` détruit (ajouter de la latence vidéo sans toucher à
   l'audio). `track3` §5.1 exige d'ailleurs une capture optique 240 fps ; `measure/` ne
   l'implémente pas. **Les deux pistes ne mesurent pas la même chose.** La méthode optique
   est la seule qui voie l'image ; elle est décrite et jamais faite.
2. **Point d'écoute unique, et 5 sources.** Le groupe joue sur 5 enceintes réparties ;
   `pair()`/`onsets()` prend la **première** arrivée au-dessus du seuil, donc l'enceinte la
   plus proche du micro. Ce n'est pas « l'offset perçu » mais « le meilleur cas à cet endroit ».
   `measurement-protocol.md` L92-99 l'admet à moitié ; il faudrait au minimum mesurer à 2–3
   positions et rapporter la dispersion spatiale.
3. **Fréquences harmoniquement liées : 1 kHz / 2 kHz.** Le 2ᵉ harmonique d'un bip 1 kHz
   **tombe exactement dans la bande cuisine** (`band_envelope` : ±1 bin = ±47 Hz autour de
   2000 Hz). Un haut-parleur de TV non linéaire à 0.4 de volume produit ce harmonique ; il
   fabrique alors un faux « onset cuisine » à l'instant du bip TV ⇒ offset mesuré ≈ 0 ⇒
   **faux PASS**. Le self-test ne peut pas le voir (sinusoïdes pures, aucune distorsion).
   Il faut des fréquences non commensurables (ex. 800 Hz et 2310 Hz).
4. **Le signal de mesure doit maintenant exister : il existe depuis 01:21** (`assets/click_train_2k.wav`,
   720 s, 2 kHz, phase 0). Mais `phase=0` suppose que le bip de la vidéo de test tombe sur la
   même grille ; sinon l'erreur de phase se lit comme un offset (§2.4), et le mode
   `--measure-tone` du relais reste **à implémenter** (le protocole le dit lui-même).

### 2.4 Ce que l'analyseur ne sait pas faire (démontré, pas supposé)

J'ai réutilisé **son propre générateur** (`selftest_analyzer.synth`) pour sortir du domaine
testé :

```
offsets vrais      mesuré     verdict rendu
  +150 ms          +150.0     PASS   (limite inclusive)
  +550 ms          +550.0     FAIL
  +600 ms          −400.0     FAIL    <-- repliement de 1000 ms (période du bip)
  +900 ms          −100.0     PASS    <-- FAUX PASS, erreur réelle 1000 ms
```

`pair(max_gap=0.6)` (`analyze_offset.py` L79-94) apparie chaque bip TV au clic cuisine le plus
proche **dans 0.6 s** ; au-delà de 600 ms, il apparie avec le clic **voisin** et retourne une
valeur décalée de −1000 ms, **sans erreur**. Avec un bip toutes les 1 s, la zone d'ambiguïté
commence à 600 ms, c'est-à-dire dans la plage où l'on attend le **premier** calibrage
(`--delta 0`, offset inconnu). Le mot « sync word » 3-clics de `make_assets.py` L4-5, censé
lever l'ambiguïté, **n'est jamais utilisé** par l'analyseur.

Second test, avec un « lit musical » continu contenant de l'énergie à 1 kHz **et** 2 kHz
(au lieu du lit à 90 Hz du self-test) :

```
offset vrai   mesuré   paires
     0 ms     None       0     <-- aucune mesure ("no paired onsets")
   +60 ms     +62.0      52
  +150 ms    +152.0      52
```

Donc : dès que du contenu réel occupe les bandes de mesure, la détection est fragile, et à
offset nul elle échoue complètement. Le self-test ne couvre ni ce cas, ni la distorsion, ni
le repliement, ni le seuil 150 ms lui-même (max testé : 120 ms).

Enfin, `analyze_offset.py` L125-126 : le verdict est `PASS` si **|médiane| ≤ 150 ms**. Une
mesure à médiane 0 ms avec ±200 ms de dispersion passerait. Aucun test de dispersion dans le
verdict (le protocole §5 ajoute 40 ms σ, le code ne l'applique pas).

---

## 3. L'éléphant : `delta` constant est-il une hypothèse défendable ?

La boucle est **ouverte par rapport à la grandeur promise**. Elle asservit
`tête_groupe − tête_TV` (rapporté par CASTv2), qui ne contient **aucune** des deux latences de
rendu. Tout le critère « ≤ 150 ms perçu » tient donc dans une constante `delta` mesurée
**une fois**, et **rien dans le système en marche ne peut détecter qu'elle est fausse** — S3 le
montre : avec `delta=200 ms`, la boucle est parfaitement contente (moy. 1.8 ms).

### 3.1 Ce qui rend `delta` instable

| Source | Effet plausible | Preuve/état |
|---|---|---|
| Mode image de la TV (Jeu/Cinéma, interpolation) | **+40 à +150 ms de latence vidéo** ; c'est l'option A de `track3`, et elle change `L_tv` sans changer le bip de référence | non mesuré, reconnu `track3` §5.2 |
| Format de contenu (24p/30p/60p, HDR/SDR, changement de résolution YouTube) | re-lock du refresh, changement de pipeline ⇒ saut de 16–40 ms | non mesuré, non discuté |
| Synchro interne audio/vidéo de la TV | le bip ne représente l'image qu'à cette synchro près ; elle dépend du mode image | **hypothèse fondatrice non mesurée** (§2.3-1) |
| Tampon du groupe (rebuffer, re-négociation) | la latence de rendu du groupe bouge, et **c'est l'acteur (SEEK/rate) qui la perturbe** | non mesuré ; Google ne publie que des écarts relatifs 0–40 ms (`track3` F3) |
| Changement de codec entre calibration et réel | le protocole mesure sur des **clics WAV** et joue ensuite du **m4a/AAC** : latence de décodeur/buffer différente | non mesuré, et le garde-fou proposé est un test nul (M-P) |
| Charge Wi-Fi / RTT | la staleness du snapshot TV (jusqu'à ~100 ms, RTT max 192 ms) est **absorbée dans `delta`** au calibrage ; une variation de RTT ensuite décale l'offset perçu de la même quantité | RTT mesurés : méd. 10 ms, p90 20, **max 192** (`07`, n=64) |
| Position d'écoute | 2.9 ms/m ; 5 enceintes ⇒ la « première arrivée » change avec la place | non discuté au-delà de §6 |

### 3.2 Ce qu'il faudrait par session (et qui n'existe pas)

- **Une mesure acoustique au début, au milieu et à la fin** de chaque session ≥ 10 min, avec
  la **position du micro identique** — c'est ce que dit le protocole §3-4, et c'est le minimum.
- **Les conditions de rendu notées à chaque mesure** : mode image de la TV, résolution/refresh
  du contenu, ad ou pas, nombre de membres du groupe actifs.
- **Une mesure optique** (240 fps) au moins une fois, pour valider l'hypothèse
  « bip TV ≡ image » — sinon `delta` mesure une convention, pas la perception.
- **Un contrôle de dérive indépendant de `delta`** : l'offset numérique ne suffit pas
  (il ignore les latences de rendu) ; l'acoustique seule le donne.

Tant que `delta` n'est pas mesuré, le PoC ne peut pas distinguer « aligné à 20 ms » de
« aligné à 300 ms ». `relay/cast_relay.py` L22-23 le dit honnêtement ; le README du conteneur
(L10) présente l'objectif comme une propriété du service.

---

## 4. Désaccords à conserver (non tranchés par les preuves)

1. **Signe de `delta`.** `measurement-protocol.md` L26 pose `delta = L_tv − L_grp` ; le code
   vise `tête_groupe = tête_TV + delta` (`cast_relay.py` L381). Physiquement, pour que le
   contenu entendu égale le contenu affiché, il faut `tête_groupe = tête_TV − (L_tv − L_grp)`.
   Le §4 du protocole calibre par `delta = −OBS`, ce qui est **correct** pour le code mais
   **contredit** le §0 et la docstring. Aujourd'hui c'est sauvé par la calibration empirique ;
   si quelqu'un « corrige » le code d'après la doc, il inverse l'erreur. À trancher par écrit,
   avec un schéma de signes.
2. **`PLAYBACK_RATE` : levier réel ou écho ?** Non annoncé dans `supportedMediaCommands`
   (bit 2048 absent), « accepté » et un ratio mesuré à ±0.23 %. Le levier fin de toute la
   stratégie anti-dérive repose là-dessus. À re-mesurer sur ≥ 60 s et à 0.995 **et** 1.005,
   avec une incertitude.
3. **« Impossible sans certificat Google »** : vrai probablement, non démontré par 11
   (§2.1), et sous-argumenté (binaire du récepteur YouTube).
4. **Latence de démarrage** : 0.40 s (sim) / 0.50 s (`SEEK_LEAD_S`) / **0.59–0.65 s** (mesures).
   Les trois ne peuvent pas être vrais ; les notes ne choisissent pas.
5. **Dérive** : 91 ppm (une mesure à 0.48σ sur 20 s) traitée comme un fait, alors que le
   critère du protocole est **≤ 60 ms/10 min**. Pour obtenir ±20 ms/10 min il faut ~10 min de
   données (l'erreur-type varie en T^(−3/2) : 191 ppm à 20 s ⇒ ~1 ppm à 600 s). Personne ne
   l'a fait ; la conclusion « sous le budget » n'est pas atteignable avec 20 s.
6. **Cause de l'interruption de la TV** : corrélation solide (08 : la TV exécute le DMR du
   groupe), mécanisme non tracé (pas de timeline avant/après sur .80).
7. **Ce que les tests ont réellement produit comme son.** Tous les tests matériels ont été
   « muets » : 5 Nest mis à `level=0, muted=true`, **mais** (a) le volume du **groupe** restait
   à 0.069–0.29 (`06` `group_receiver_status.volume.level=0.069` ; 08 n'enregistre pas le
   volume du groupe), et (b) la **TV** n'a jamais été mute (`silence(touch_tv=False)` :
   `touch_tv` n'est utilisé nulle part ; `group_load_test.py` ne mute que les 5 enceintes), alors qu'elle était membre
   du groupe joué, à `level=0.4, muted=false` (`04`, `05`). Un Chromecast qui exécute un DMR
   avec un flux BUFFERED en autoplay joue le son. **Il est très probable que les tests A3–A5
   aient fait jouer de l'audio sur la TV du salon.** Aucune preuve ne le contredit et aucune
   mesure de mute n'a été prise *pendant* la lecture. C'est, à mon avis, le point le plus
   grave du dossier — au-delà de la technique.
8. **Périmètre de l'incident** : 3 groupes touchés en 08 (pas 2), et `rollback.md` minimise.

---

## 5. Preuves manquantes, par critère d'acceptation

| Critère | Ce qui existe | Ce qui manque |
|---|---|---|
| offset perçu ≤ 150 ms | analyseur validé sur synthétique (14) ; **aucun** `evidence/av-0*.json` | 3 enregistrements réels ; le signal 2 kHz ; le mode mesure ; un groupe cuisine-only |
| pas de dérive sur ≥ 10 min | 20 s de tête de lecture (07), 15 s de lecture groupe (06), 72 s simulés | **toute** mesure ≥ 10 min ; une incertitude sur la pente |
| lecture / pause / seek / volume / stop | seek+rate sur groupe (07,08) ; le reste **seulement en simulation** (13) | pause/reprise sur le groupe, `SET_VOLUME` sur le groupe, un vrai `PLAY` après `PAUSE` |
| enchaînement auto / fin de session / nouvelle session | S1 simule `stop`+`newsession` (13) | rien sur matériel ; le chemin `fetch_async` + `sink.stop()` (silence pendant le `yt-dlp`) n'est ni mesuré ni documenté ; les pubs YouTube ne sont pas traitées |
| rollback | `15-panic-test.log` (état, sans valeurs) + `emergency.py` | un vrai test avant/après avec volumes, depuis une session **en cours** ; la preuve que le mute tient pendant une lecture de groupe |
| TV jamais commandée | code : `RemoteTv` n'envoie que `GET_STATUS`/`CONNECT` ✓ | aucune observation read-only de 10 min ; le comportement de reconnexion à 4 Hz (§2.2) |
| groupe cuisine-only | n'existe pas | sa création (propriétaire) ; re-mesure de A4/A5 dessus |

---

## 6. Défauts d'intégrité des preuves (à corriger avant tout consensus)

1. **`05-device-snapshot.log` et `.json` ne sont pas de la même exécution**, et les deux
   fichiers portent le même nom : le `.log` (00:34:24) montre la TV sur YouTube `PLAYING
   t=1675.659` ; le `.json` (00:37:57) montre la TV sur « Backdrop » avec un timeout, et
   `Fenetre Vert` à `volume 0.0` au lieu de 0.0833. Aucun des deux n'est horodaté ni ne
   versionne la source. C'est la seule preuve d'observation TV, et elle est ambiguë.
2. **`evidence/09` n'a pas de générateur dans le dépôt** (`grep isVideoGroup` : uniquement
   dans `evidence/`). La preuve la plus critique pour la sécurité n'est pas reproductible.
   Au passage, `09` affiche `isVideoGroup: false` pour « Dehors », qui ne contient **que**
   deux appareils vidéo : ce champ ne veut pas dire ce qu'il a l'air de dire.
3. **`evidence/08` : `members: []`** pour les 3 groupes. `who_runs()` compare
   `transport == tr` du groupe, or chaque membre a son propre transport (`06`
   `same_session: false` partout) : le drapeau `member` est structurellement toujours faux.
   Le fichier ne prouve donc **pas** l'appartenance (09 le fait).
4. **`02` et `03` sont des runs cassés** (`AttributeError: 'MediaStatus' object has no
   attribute 'media_info'`, `UnboundLocalError`, 3× `RequestTimeout`) — non cités par les
   notes, mais présents dans `evidence/`. `02` attribue même le nom « Cuisine Meuble Bleu » à
   l'hôte `.80` : ne jamais déduire une identité d'appareil d'un nom pychromecast.
5. **`13` et `14` sont reproductibles** (vérifié : mêmes chiffres à ≤0.1 ms) — c'est le point
   fort du dossier ; il faut juste cesser de les présenter comme une validation du réel.
6. **`relay-*.jsonl` vides** : **deux** essais du relais réel (`01350` à 01:13:50 et `012111` à
   01:21:11) ont laissé 0 octet. Ces fichiers devraient être supprimés ou accompagnés d'une
   note expliquant l'échec, sinon ils ressemblent à la preuve d'un non-fonctionnement.
7. **`rollback.md` date un test à 01:38** alors que son artefact est de 01:17, et `00-labo`
   A10 reprend la même affirmation. Une note ne peut pas dater dans le futur une preuve déjà
   écrite.

---

## 6.1 Points du `consensus.md` provisoire (01:26) à corriger avant de le figer

Je ne réécris pas le consensus ; je signale seulement les phrases qui héritent d'affirmations
réfutées ci-dessus.

| Consensus | Problème |
|---|---|
| §2 L25 : « `evidence/04` : la session YouTube de la TV est lisible **en continu** (id vidéo, tête de lecture, état) » | 04 est un **timeout** ; la seule lecture réussie est un échantillon dans `05.log`. « en continu » n'a jamais été démontré (et les deux `relay-*.jsonl` sont vides). |
| §2 L24 : « n'envoie **jamais** `CONNECT` (0 sur ~15 tentatives) » | Même réserve que A7 : 12 connexions de `.2`, 14 de `.115` ; `sender_disconnected=0` est un artefact de journal. |
| §2 L25 : « `evidence/13` : la boucle tient l'erreur < 50 ms …, y compris avec une dérive **55× pire que la réelle** » | Le chiffre < 50 ms est **filtré** (max non filtré 585 s) ; et S1 (91 ppm, 72 s) n'exerce **pas** la dérive : 6.6 ms cumulés, sous le seuil mort de 30 ms. La dérive n'est testée qu'à 3000 ppm. |
| §3 L39-40 : « `SET_PLAYBACK_RATE` … mesuré honoré, cadence du groupe 1.000091 → 55 ms par 10 min » | Le levier n'est **pas annoncé** par l'appareil (bit 2048 absent de 274447), son gain n'est connu qu'à ±0.23 %, et 1.000091 vaut ±114 ms/10 min à 1σ. À formuler comme des hypothèses à mesurer, pas comme des acquis. |
| §3 L41 : « `delta = latence_TV − latence_groupe` » | **Signe faux** (cf. §4.1). C'est le paramètre central du consensus ; l'erreur se propage. |
| §3 L42-43 : « instrument validé à ±4 ms … ±0.1 ms/min (`evidence/14`) » | Vrai **dans le domaine testé** (synthétique, |offset| ≤ 120 ms, lit à 90 Hz) ; démontré faux au-delà (repliement à 600 ms avec faux PASS ; échec avec de l'énergie dans les bandes). |
| §4.3 L55 : « **Deux** tests l'ont fait » | Au moins **quatre** prises de contrôle de la TV (06, 07, + les trois groupes de 08). |
| §4.4 L57-58 : « la latence de démarrage diffère de **70 ms** entre WAV et M4A (`evidence/16`) » | Chiffre exact (0.542 vs 0.612 s) mais **n=1 par format**, sur **une seule enceinte (.61)**, et les deux essais ne sont pas dans le même état (`state_at_load` = IDLE vs PLAYING). Rien ne dit que l'écart se transpose au groupe. Bonne décision, preuve trop mince pour figer un choix. |
| §5 : désaccords déjà listés | Cohérents avec mon §4, sauf qu'il manque les deux plus lourds : (a) rien ne prouve encore que le relais peut **lire la tête de lecture de la TV de façon fiable** (1 succès, 1 timeout, 2 journaux vides) ; (b) les tests matériels ont probablement fait **jouer la TV** (§4.7). |

---

## 6.2 Reproductibilité de mes propres tests

Fait dans une copie (`tar` de tout sauf `.venv`/`bin`, `.venv` symbolique) pour ne rien
écraser. Les deux essais du §2.4 recopient **le générateur du projet** :

```python
import sys; sys.path.insert(0, "measure")
from selftest_analyzer import synth, write_wav
from analyze_offset import analyze
for off in (150, 550, 600, 900):                       # offsets hors du domaine testé
    write_wav(f"/tmp/r{off}.wav", synth(float(off), 0.0, 60.0, drop_every=7))
    print(off, analyze(f"/tmp/r{off}.wav")["offset_median_ms"])
# +600 -> -400.0 ; +900 -> -100.0 (verdict PASS, erreur réelle 1000 ms)
```

Pour le « lit musical » : ajouter à `synth(...)` une porteuse 1 kHz + 2 kHz modulée en
amplitude (0.10, AM 0.13 Hz) avant normalisation ; à offset 0 le résultat est
`pairs=0` (« no paired onsets »), à +60 ms `62.0`, à +150 ms `152.0`.

`measure/selftest_analyzer.py` et `relay/sim.py` rejoués tels quels : résultats identiques à
`evidence/14` et `evidence/13` (≤0.1 ms). Ces deux-là sont donc reproductibles — ce qui est
rare dans ce dossier et mérite d'être dit.

---

## 7. Ce que je n'ai pas fait / pas vérifié

- Aucun paquet vers un Cast (ni `.80` ni un groupe) : tout est statique ou local.
- Les sources externes de `track3` (support.google, WiiM, B&H, prix) ne sont pas vérifiées.
- Le comportement de `MULTIZONE_STATUS` sur un futur groupe cuisine-only est inconnu : la
  règle `caps == 196612` pourrait aussi exclure un Chromecast Audio légitime, ou inclure un
  appareil vidéo mal typé. La garde est utile mais non testée sur un groupe neuf.
- La spec exacte du leg `receiver → sender` de deviceauth (hypothèse §2.1-3) : je ne l'ai pas
  vérifiée dans une implémentation de référence ; c'est une piste à instruire, pas un fait.
