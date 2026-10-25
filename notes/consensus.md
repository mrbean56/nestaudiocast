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

## 6. Ce qui reste à faire avant de déclarer le PoC terminé

1. ~~Créer le groupe cuisine-only~~ — **fait** le 2026-10-25 (groupe « Cuisine », 5 Nest Audio,
   aucun appareil vidéo) ; le relais le résout par nom et la garde l'accepte.
2. ~~Prouver la lecture continue de la TV~~ — **fait** (sessions de 344 s, 413 s, 593 s).
3. **Mesurer l'offset perçu** à 3 moments d'une session ≥ 10 min : **bloqué** sur l'accès au
   microphone du téléphone (débogage sans fil coupé). Tout est prêt : `tools/acoustic_session.py`
   avec `IP:PORT`, instrument validé hors ligne (±4 ms, biais +6 ms à soustraire).
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

