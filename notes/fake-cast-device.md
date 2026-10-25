# Peut-on fabriquer une cible Cast ? (piste A, reprise)

Question posée le 2026-10-25 : « et si je voulais, en tant que fabricant d'appareils, que
mon appareil accepte les casts ? Sinon, j'ai un Chromecast de rechange, peut-on le bricoler ? »

Ce document sépare **ce qui est mesuré ici** de **ce qui est documenté ailleurs**, et donne
les trois voies réelles.

---

## 1. Ce que le LAN a prouvé (mesure, pas opinion)

Un récepteur CASTv2 complet a été écrit (`probe/virtual_receiver.py`) : annonce mDNS
`_googlecast._tcp` avec les clés réelles, serveur TLS sur 8009, messages `CastMessage`
protobuf, namespaces `receiver`/`media`/`tp.connection`/`tp.heartbeat`/`tp.deviceauth`, et un
Default Media Receiver fonctionnel (LOAD, PLAY, PAUSE, SEEK, SET_PLAYBACK_RATE, tête de lecture).

- Avec un expéditeur qui ne valide pas les certificats (notre client), **tout fonctionne** :
  8 étapes sur 8 (`evidence/12`).
- Avec un **véritable expéditeur Google** présent sur le réseau (poste `192.168.2.2`, pile Cast
  de Chrome) : TLS OK, challenge `deviceauth` reçu, notre `AUTH_REPLY` (certificat auto-signé +
  signature valide du nonce) envoyé… puis **fermeture propre, sans jamais envoyer `CONNECT`**
  — 12 fois de suite (`evidence/11`). Aucune tentative de `LAUNCH`, donc aucune app YouTube.

**Conclusion mesurée** : le blocage est à l'authentification de l'appareil, pas dans le
protocole applicatif. Notre réponse est cryptographiquement correcte (signature vérifiée avec
notre clé publique, protobuf relu sans perte) ; ce qui manque est une **chaîne de certificats
signée par Google**.

Deux causes restent théoriquement possibles (chaîne non signée, ou forme de réponse non
conforme aux attentes du parseur Google), mais la seconde est improbable et, de toute façon,
les deux sont également bloquantes. Le test qui trancherait serait de lire le motif d'échec en
clair dans `chrome://media-router-internals` sur le poste qui tente la connexion : **non fait**.

À noter : même avec un certificat valide, il faudrait encore **le binaire du récepteur YouTube**
(`233637DE`), qui n'est pas distribué publiquement. Le certificat est nécessaire, pas suffisant.

## 2. Comment un vrai fabricant fait (voie officielle)

1. **Programme de certification Google Cast.** Le fabricant construit un récepteur avec le
   Cast SDK, passe la certification, et reçoit un **certificat d'appareil + clé privée émis par
   Google**, stockés dans une zone sécurisée du produit. C'est ce couple qui répond au
   `deviceauth` et que l'expéditeur (YouTube, Chrome, Spotify…) vérifie contre la racine Google.
2. **Licence « Google Cast for Audio »** pour les enceintes : même principe, l'appareil est
   certifié et son certificat est provisionné en usine.
3. **Android TV / Google TV** : le fabricant n'écrit pas le récepteur, il embarque celui de
   Google. C'est la voie la plus simple pour « un appareil qui accepte les casts ».

Aucune de ces trois voies n'est accessible à un particulier : la clé est émise par Google à un
partenaire certifié. C'est une barrière **cryptographique**, pas une limitation technique du
protocole.

## 3. Ce qu'un particulier peut réellement faire

### 3.1 Récepteur personnalisé sur un vrai Chromecast (le Chromecast de rechange)

C'est la voie **légitime et immédiate**, et elle ne demande pas de casser quoi que ce soit :

- créer un compte Google Cast Developer (inscription unique, ~5 $ US), enregistrer une **URL de
  récepteur** hébergée chez soi (une page HTML qui utilise le Cast Receiver SDK) et obtenir un
  **APP_ID** ;
- sur le Chromecast de rechange, lancer cet APP_ID (par l'API `receiver` en CASTv2, comme le
  fait n'importe quelle app de cast) ;
- il devient alors une cible de cast **pour notre propre expéditeur** (une page web, un script),
  avec un flux qu'on choisit : HLS/DASH servi par le conteneur, vidéo + audio.

Ce que ça donne : « je caste **mon** flux vers **mon** récepteur ». Ce que ça ne donne pas :
l'app YouTube officielle ne peut pas viser un récepteur tiers — elle est câblée sur son propre
récepteur et sur l'identité d'appareil Google.

C'est précisément la voie **« sender maison »** autorisée par le cahier des charges, et elle a
un avantage réel sur l'architecture actuelle : **on possède les deux sessions** (vidéo et audio),
donc on choisit leur départ au lieu de subir celui de la TV.

### 3.2 Bricoler/root un Chromecast

- Chromecast 1re génération : des exploits matériels existent, la communauté a pu rooter et
  remplacer le firmware.
- Chromecast 2/3/Ultra/HD (les nôtres) : démarrage vérifié, pas d'exploit public connu.
- Même rooté : le `deviceauth` exige toujours la clé Google, et l'app YouTube exige son
  récepteur. Le root ne débloque donc pas la cible de cast ; il ne servirait qu'à faire tourner
  notre propre récepteur, ce que la voie 3.1 permet déjà **sans** rooter.

### 3.3 Récepteurs open source

Des projets existent (`nodecast-js`, le « virtual device » de `pychromecast`). Ils reproduisent
le protocole, et ne passent pas l'authentification : c'est exactement ce que la mesure du §1
montre, avec nos propres chiffres.

## 4. Recommandation

| Objectif | Voie | Coût | Ce qu'on obtient |
|---|---|---|---|
| « caster depuis l'app YouTube vers mon appareil » | **impossible** sans certificat Google | — | — |
| « caster mon propre flux vers mon propre appareil » | récepteur personnalisé (§3.1) | ~5 $ US + le Chromecast de rechange | deux sessions maîtrisées, donc un vrai contrôle du décalage |
| « garder l'app YouTube et synchroniser la cuisine » | architecture actuelle (relais observateur) | 0 $ | mesuré : −30 ms ± 3 ms |

Le Chromecast de rechange a donc une valeur : il permet de **libérer la TV du salon** (le PoC
tourne sur un écran de banc) et d'essayer la voie « sender maison » avec un flux propre, sans
toucher au téléviseur de la famille. Il ne permet pas de récupérer l'app YouTube comme source.

## 5. Prochaine expérience (peu coûteuse, réversible)

1. Brancher le Chromecast de rechange sur le LAN, noter son IP (découverte mDNS).
2. Créer le compte Cast Developer, enregistrer une URL de récepteur servie par CT 121.
3. Lancer cet APP_ID sur le Chromecast de rechange **depuis le conteneur** (lecture seule pour
   le reste du foyer), charger un HLS local, et vérifier qu'il joue.
4. Si oui : mesurer le décalage entre ce flux vidéo et le groupe cuisine, avec le même protocole
   que pour la TV (`notes/measurement-protocol.md`).

Étape 2 demande une action du propriétaire (création du compte et enregistrement de l'URL) —
c'est la seule partie non automatisable.
