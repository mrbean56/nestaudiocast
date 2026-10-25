# Track 3 — Voie matérielle ≤ 100 $ CA

Recherche seule. **Aucun matériel n'est présent sur le LAN, donc aucune valeur ci-dessous n'est mesurée.** Toute latence annoncée est une spécification, une valeur constructeur ou une observation de forum — jamais une mesure. Prix vérifiés le **2026-10-25**, en CAD, taxes et livraison exclues sauf mention.

Objectif rappelé : YouTube en vidéo sur « Chromecast Salon » (192.168.2.80, Chromecast 3e gén.) + **le même audio sur le groupe Nest Audio cuisine** (192.168.2.61-65), offset ≤ 150 ms stable, sans dérive sur ≥ 10 min.

---

## VERDICT

**Aucun produit ≤ 100 $ CA ne peut atteindre l'objectif tel qu'énoncé.** Ce n'est pas une question de prix, c'est structurel : le groupe Nest Audio n'a **aucune entrée audio**, donc aucun boîtier de délai ne peut être inséré dans sa voie. Les seuls leviers matériels restants sont (a) retarder la vidéo — matériel professionnel, ≥ 300 $ US, ou (b) remplacer la sortie cuisine — ce qui n'est plus « le groupe Nest Audio ».

Le seul candidat à 0 $ : **jouer sur la latence vidéo du téléviseur** (mode image non-jeu / traitement d'image). Ça ne peut qu'**ajouter** de la latence vidéo, donc ça ne marche que si l'audio du groupe est **en retard** sur l'image — le cas probable — et c'est grossier, non gradué en ms, dépendant du modèle de TV. À mesurer, non garanti.

Le moins cher qui peut réellement livrer ≤ 150 ms mesuré : **remplacer l'endpoint cuisine** par Pi Zero 2 W + DAC/amp + petite enceinte alimentée, alimenté par un extracteur HDMI sur la TV, avec latence réglable au ms → **≈ 110-130 $ CA** (BOM plus bas). Ce n'est plus le groupe Nest.

---

## 1. Les trois faits qui bloquent la voie matérielle

| # | Fait | Statut | Source |
|---|---|---|---|
| F1 | **Nest Audio n'a aucune entrée audio.** Fiche officielle, rubrique « Ports » : *DC power jack*, rien d'autre. Pas de 3,5 mm, pas d'entrée ligne, pas de Bluetooth A2DP en réception. | [documented] | support.google.com/googlehome/answer/7072284 (consulté 2026-10-25) |
| F2 | **Un groupe Google Cast n'accepte que de l'audio réseau.** « Google Cast groups support only network audio input and cannot transmit audio from other inputs (e.g., Line In, Optical In, HDMI ARC, or Bluetooth) to devices in the group over Wi-Fi. » Confirmé mot pour mot dans le manuel WiiM Pro v1.9.5. Donc même un appareil Cast **avec** entrée ligne (WiiM Pro, Google Home Max) ne peut pas injecter un signal retardé dans le groupe. | [documented] | faq.wiimhome.com/.../72000596652 ; wiimhome.com/langPdf/en/WiiM Pro UM.pdf |
| F3 | **Le réglage « Group delay correction » de Google ne synchronise que les membres du groupe entre eux**, jamais par rapport à une vidéo : « This setting will have no effect when casting to a single speaker or TV. » Google documente d'ailleurs des délais *relatifs* typiques : enceintes 0-40 ms, barres 0-80 ms, récepteurs 0-70 ms. Aucune valeur absolue groupe↔vidéo n'est publiée. | [documented] | support.google.com/chromecast/answer/6318642 |

Conséquence : F1 + F2 tuent tout boîtier de délai audio, quel que soit son prix. Reste F4.

| F4 | **Retarder la vidéo** exige un vrai délai HDMI. Le seul matériel qui le fait est du broadcast/pro (B&H, catégorie Video Delays : à partir de ~312 $ US ; les modèles à offset A/V réglable dépassent 1 000 $ US). Le Lumagen RadiancePro est cité comme « le seul produit HDMI qui acknowledge le délai vidéo ». | [documented] spec / [anecdote] prix plancher | bhphotovideo.com/c/buy/Video-Delays/ci/5721 ; audiosciencereview.com forum thread 27739 |

---

## 2. Tableau des options

| Option | Produit (réf.) | Prix CAD (2026-10-25) | Mécanisme de sync | Latence attendue | Source |
|---|---|---|---|---|---|
| A. Latence vidéo par mode image | TV existante, mode « Cinéma » / traitement d'image activé (sortir du mode Jeu) | **0 $** | On ajoute de la latence vidéo pour rattraper un audio en retard | +40 à +150 ms selon modèle, **non gradué**, à mesurer | [à mesurer] — pas de doc constructeur pour ce modèle de TV |
| B. Boîtier délai audio (analogique) | Sescom **SES-A-V-SYNC** (RCA L/R, 0-300 ms par pas) | ~275 $ (≈199 $ US) | Retarde l'audio de 0-300 ms, pas de 1-50 ms | Précision ms, **inutilisable ici** : impossible d'entrer dans le groupe (F2) | [documented] sescom.com/.../ses-a-v-sync ; prix [anecdote] avis Amazon US |
| C. Délai vidéo professionnel | Boîtiers B&H « Video Delays » | dès ~430 $ (312 $ US) | Tampon de trames HDMI/SDI | Oui, réglable — **hors budget** | [documented] bhphotovideo.com/c/buy/Video-Delays/ci/5721 |
| D. Chromecast Audio usagé | Google **RUX-J42** | 60 $ (Marketplace Montréal, usagé) à 113 $ + 31 $ livr. (eBay.ca) | Devient membre du groupe Cast | **Aucune entrée ligne : ne sert à rien** pour injecter un délai ; ne fait qu'ajouter une enceinte | [anecdote] ebay.ca/p/10062493797 ; facebook.com/marketplace/quebec ; spec sortie 3,5 mm seulement |
| E. Streamer Cast avec aux-in | **WiiM Pro / Pro Plus** | 359 $ (Amazon.ca, Vinyl Sound) | Aux-in → mais **ne propage pas** vers un groupe Google Cast (F2) | N/A pour le groupe | [documented] wiimhome FAQ + manuel ; prix amazon.ca + vinylsound.ca |
| F. Endpoint maison réglable | **Pi Zero 2 W + DAC/amp I2S + enceinte alimentée + extracteur HDMI** | **≈ 110-130 $** (BOM §4) | Latence client réglable au ms (Snapcast / Music Assistant), dérive < 0,2 ms | Délai arbitraire, déterministe → **≤ 150 ms atteignable** | [documented] github.com/snapcast/snapcast ; home-assistant.io/actions/snapcast.set_latency ; music-assistant.io/player-support/snapcast |
| G. Mettre le Chromecast vidéo dans le groupe | Chromecast 3e gén. / Google TV Streamer | 0 $ (déjà possédé) | Google synchronise les membres du groupe | **Musique seulement** : l'audio d'une vidéo ne sort pas vers le groupe | [documented] support.google.com/chromecast/answer/6318642 (liste Chromecast/Google TV Streamer comme appareils de lecture de groupe) ; [anecdote] reddit r/googlehome/zoa5u0 |

Notes de prix (toutes 2026-10-25) :
- Extracteur HDMI MOSWAG 4K60 (HDMI→HDMI + SPDIF + 3,5 mm) : **25,99 $** — amazon.ca/hdmi-audio-extractor-1080p/s?k=hdmi+audio+extractor+1080p
- Extracteur Amazon Basics 4K30 : **32,99 $** — amazon.ca/AmazonBasics-Audio-Stereo-Extractor-Converter/dp/B07KRWYN4R
- Pi Zero 2 W : **21,95 $**, SKU 1799 — pishop.ca/product/raspberry-pi-zero-2-w/ (**en rupture** selon les métadonnées de la page ; MSRP 15 $ US, pénurie chronique depuis 2026-09)
- MAX98357A (I2S, 3 W, ampli intégré) : **31,49 $** (Amazon.ca, vendeur unique, cher — équivalent AliExpress ~8-12 $)
- Enceinte alimentée Creative Pebble 2.0 (3,5 mm) : **27,63 $** — amazon.ca/Creative-USB-Powered-Speakers-Far-Field-Radiators/dp/B0791H74NT
- microSD 32 Go + alim 5 V/2,5 A : ~22 $ combiné (estimation, non vérifiée à la pièce)

---

## 3. Réponses explicites aux 5 questions

**Q1 — Existe-t-il un produit qui retarde une des deux voies, réglable, ≤ 100 $ ?**
Pour l'audio : oui, ça existe (Sescom SES-A-V-SYNC, 0-300 ms ; DSP ADAU1701/ESP32+I2S à ~15-30 $ en DIY) — mais **inutilisable** ici, voir F1/F2. Pour la vidéo : **non**. Aucun produit grand public ≤ 100 $ ne retarde une image HDMI de façon réglable ; le plancher documenté est ~312 $ US chez B&H. [documented]

**Q2 — Peut-on alimenter le groupe Nest Audio depuis une source ligne (le son de la TV) ?**
**Non, documenté.** (a) Chromecast Audio est **sortie seulement** (pas d'entrée) et discontinué depuis janvier 2019 ; (b) WiiM, le seul streamer Cast courant avec aux-in, écrit noir sur blanc que les entrées physiques (Line-In, SPDIF, HDMI ARC, BT) **ne peuvent pas** être transmises à un groupe Google Cast ; (c) seul haut-parleur Nest avec entrée analogique : **Google Home Max** (3,5 mm analog input, fiche officielle), discontinué, et l'aux-in ne rejoint pas le groupe non plus. Donc : **aucun produit, à aucun prix**, ne fait entrer un signal ligne dans le groupe. [documented]

**Q3 — Un Chromecast vidéo peut-il être dans le même groupe qu'un Nest Audio, avec sync acceptable ?**
Nuancé, et à vérifier sur place :
- [documented] La page « corriger le délai de lecture du groupe » liste explicitement **Chromecast, Chromecast Audio, Chromecast with Google TV, Google TV Streamer (4K)** comme appareils pouvant jouer dans un groupe, avec le réglage de correction de délai par appareil.
- [documented] Mais la page « Créer et gérer des groupes d'enceintes » n'énumère, sous « Compatible devices », que des **enceintes et écrans** ; les appareils de streaming n'y figurent plus.
- [anecdote] Depuis juin 2024 plusieurs utilisateurs rapportent que Google a **retiré** les Chromecast des groupes (r/googlehome 1dydq0u, 1epmrf0). Depuis 2024, un appareil ne peut appartenir qu'à **un seul** groupe (contrainte liée au litige Sonos).
- Surtout : même quand ça marche, **c'est de la musique**. Un cast **vidéo** sur le Chromecast ne renvoie pas son audio vers le groupe [anecdote, cohérent avec F2]. Le groupe ne transporte que des flux audio réseau.
→ Conclusion : **ne résout pas le cas YouTube**. Utile uniquement si on accepte « musique sur TV + groupe ».

**Q4 — Alternative Raspberry Pi : un Pi peut-il se faire passer pour une cible Cast ?**
- **Non, pas légitimement.** Aucune implémentation open-source maintenue de *récepteur* Cast : l'authentification exige le certificat/clé d'appareil Google. C'est l'avis du mainteneur de Music Assistant (discussion #2354) et la raison pour laquelle ces projets restent expérimentaux. [documented]
- Donc le Pi serait **un sink à nous**, piloté par notre propre relais (Snapcast, ou Music Assistant/Sendspin), pas une cible Cast.
- Ce que ça donne : latence **réglable au ms** par client, dérive **< 0,2 ms** (README Snapcast), buffer serveur réglable 200-6000 ms. C'est le seul mécanisme matériel qui garantit « pas de dérive sur 10 min » **par construction**.
- **BOM (CAD, 2026-10-25)** : extracteur HDMI 25,99 + Pi Zero 2 W 21,95 + microSD/alim ~22 + MAX98357A 31,49 + enceinte Pebble 27,63 = **129,06 $**. Variante économique (PCM5102 ~13 $ au lieu du MAX98357A, ou enceinte déjà possédée) : **~110 $**. Sous 100 $ seulement si on réutilise une enceinte alimentée **et** un vieux téléphone Android/Linux comme client (alors ~55 $ : extracteur + enceinte) — mais un client Snapcast Android n'est pas un composant officiel, fiabilité non garantie [anecdote].

**Q5 — Une de ces solutions peut-elle atteindre ≤ 150 ms *mesuré* en gardant la vidéo sur le renderer YouTube du TV ?**
- **Avec le groupe Nest Audio : non.** La vidéo TV ne peut pas être retardée à ce prix (F4) et l'audio du groupe ne peut pas être retardé du tout (F1/F2). Il ne reste que l'option A (mode image), qui n'ajoute que de la latence vidéo : elle ne peut corriger que si **audio en retard**, dans la fenêtre que la TV offre par hasard, sans granularité. Espérance de succès : faible, non quantifiable avant mesure.
- **Avec un endpoint remplacé (option F) : oui, plausiblement**, parce que le délai est alors un paramètre numérique connu et stable (Snapcast : latence par client en ms, dérive < 0,2 ms documentée). Le budget d'erreur restant est : latence d'affichage de la TV (inconnue, 10-150 ms) + latence de l'extracteur (quelques ms) + gigue réseau tamponnée. On peut annuler le tout en réglant la latence client, **à condition de mesurer l'offset réel** (voir §6). Rien de tout ça n'est mesuré ici.
- Sources de latence, qualitativement : (1) décodage/affichage du Chromecast + SoC du TV = **la plus grosse inconnue**, dominée par le mode image ; (2) buffering du groupe Cast = **non documenté en absolu** (Google ne publie que des écarts relatifs 0-40 ms entre enceintes) ; (3) démarrage des deux sessions Cast indépendantes = décalage initial arbitraire, non reproductible.

---

## 4. Risques

1. **Prix et disponibilité instables** : Pi Zero 2 W en rupture chez PiShop.ca le 2026-10-25 ; MAX98357A à 31 $ chez un vendeur unique (le même module se trouve à 8-12 $ ailleurs, livraison longue).
2. **Extracteur HDMI + HDCP** : un cast YouTube sort souvent en HDCP ; certains extracteurs bon marché refusent alors de sortir l'audio, ou forcent le downscale. À tester avant achat.
3. **Contourner le groupe Nest = changer le périmètre du PoC.** Les 5 Nest Audio ne servent plus à rien dans la cuisine ; il faut le dire au consensus, sinon on compare deux choses différentes.
4. **Option A (mode image) est un pari** : elle dépend du modèle de TV, elle dégrade l'image (interpolation), elle n'est pas réglable, et elle est invisible à toute automatisation. Elle ne peut pas être « calibrée » finement.
5. **Latence groupe non documentée** : aucune valeur officielle Google groupe↔vidéo. Toute affirmation du type « le groupe ajoute 300 ms » serait une invention tant qu'on n'a pas mesuré.
6. **Dérive** : le groupe Cast gère son horloge (pas de dérive attendue entre membres), mais deux sessions Cast indépendantes peuvent dériver l'une par rapport à l'autre (négociation de buffer, re-buffering). L'option F supprime ce risque ; les options A-G non.
7. **Chromecast dans un groupe** : comportement contradictoire entre deux pages d'aide Google, et régressions rapportées en 2024 → ne pas bâtir un PoC dessus sans test sur place.

---

## 5. Ce qu'il faudra mesurer une fois acheté (aucune de ces mesures n'existe aujourd'hui)

1. **Offset de base, avant tout achat** : filmer l'écran + les Nest Audio avec un téléphone à 240 fps, clap ou horloge milliseconde plein écran ; compter les trames entre le changement d'image et le front audio. Répéter 5 fois. → donne le signe (audio en avance ou en retard) et l'ordre de grandeur. **C'est la mesure qui décide si l'option A (0 $) a une chance.**
2. **Latence d'affichage du TV par mode image** : même méthode, mode Jeu vs mode Cinéma, 3 mesures par mode. → donne la plage de retard vidéo gratuite disponible.
3. **Stabilité 10 min** : 3 mesures à t=0, 5, 10 min. Écart-type et dérive en ms/min.
4. **Si option F** : latence client Snapcast balayée par pas de 10 ms, offset mesuré à chaque pas → courbe offset(latence) ; vérifier linéarité 1:1 et absence de dérive sur 10 min.
5. **Extracteur** : vérifier sortie audio avec contenu HDCP (cast YouTube), et mesurer sa latence propre (comparer audio extrait vs audio HP du TV, même méthode vidéo).
6. **Gigue réseau** : sur 10 min, compter les re-bufferings (logs snapclient / MA) ; un seul re-buffer = dérive visible.

---

## RÉSUMÉ POUR CONSENSUS

1. Voie matérielle ≤ 100 $ CA : **impossible** d'atteindre l'objectif avec le groupe Nest Audio. Ce n'est pas un problème de budget.
2. Blocage structurel n° 1 : le **Nest Audio n'a aucune entrée audio** (fiche Google : « Ports : DC power jack »). Aucun boîtier ne peut s'insérer dans sa voie.
3. Blocage structurel n° 2 : **un groupe Google Cast n'accepte que de l'audio réseau** ; les entrées ligne/optique/ARC/BT ne sont jamais propagées au groupe (documenté par WiiM, seul streamer Cast à aux-in).
4. Blocage structurel n° 3 : **retarder la vidéo** coûte ≥ 312 $ US (matériel pro B&H) ; rien de grand public ≤ 100 $ ne le fait.
5. Corollaire : Chromecast Audio usagé (60-113 $) **n'aide pas** — sortie seulement, pas d'entrée ; il n'ajoute qu'une enceinte.
6. WiiM Pro (359 $) **n'aide pas** non plus : aux-in non propageable vers un groupe Google Cast.
7. Mettre le Chromecast vidéo dans le groupe : [documented] possible pour de la **musique**, ne transporte **jamais** l'audio d'une vidéo → ne résout pas YouTube.
8. Seul levier à 0 $ : **mode image du TV** (retard vidéo non gradué, 40-150 ms selon modèle). Ne marche que si l'audio est en retard. À mesurer avant tout achat ; probabilité de succès faible.
9. Le moins cher qui peut livrer ≤ 150 ms **mesuré** : remplacer l'endpoint cuisine par **Pi Zero 2 W + DAC/amp I2S + enceinte + extracteur HDMI ≈ 110-130 $** (Snapcast : latence réglable au ms, dérive < 0,2 ms). Mais **ce n'est plus le groupe Nest Audio** — le périmètre du PoC change.
10. Rien n'est mesuré ici : aucune latence TV ni latence groupe n'est documentée en valeur absolue. **Première action = mesure optique 240 fps de l'offset existant**, avant toute dépense.
