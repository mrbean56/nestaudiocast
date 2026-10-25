# Audit d'acceptation — PoC « relais Cast cuisine/TV »

Chaque exigence du cahier des charges, avec la preuve exécutée correspondante. Établi le
2026-10-25 16:30. Les fichiers sont dans `evidence/`, sauf mention contraire.

## 1. Infrastructure

| Exigence | Preuve | Verdict |
|---|---|---|
| Nouveau LXC **dédié** sur pve (192.168.2.3) | CT 121 `castrelay`, 192.168.2.21, Debian 13, 2 vCPU / 1 GiB / 6 GiB, non privilégié, `onboot=1` | ✅ |
| **Léger** | 26 Mo RSS, 0 % CPU au repos | ✅ |
| **Always-on** | `systemctl is-enabled cast-relay` = enabled, `is-active` = active ; ajouté à la sauvegarde 03:45 | ✅ |
| Stack média intacte | `pct list` : 19 conteneurs dont 107 media-server, 109 nginx, 115 MariaDB, 666 dashboard, tous `running` ; aucune commande du projet ne les cible | ✅ |
| LAN only | le relais ne sort que par `yt-dlp` vers YouTube ; tout le reste est CASTv2/HTTP sur le LAN | ✅ |

## 2. La cible de cast (hypothèse principale)

| Exigence | Preuve | Verdict |
|---|---|---|
| Le conteneur se présente comme cible de cast dans l'app YouTube | récepteur CASTv2 complet + mDNS : découvert par un **vrai** expéditeur Google du LAN, mais celui-ci s'arrête à `deviceauth` et n'envoie **jamais** `CONNECT` (12/12) — `evidence/11`, `evidence/12` | ❌ **réfuté** (certificat d'appareil signé Google requis) |
| …ou une autre voie, justifiée par le consensus écrit | `notes/consensus.md` §2-3, `notes/track1-protocol.md`, `notes/fake-cast-device.md` | ✅ voie B retenue |
| Groupe Google Home **cuisine uniquement** | groupe « Cuisine », 5 Nest Audio, `caps == 196612` pour tous ; `lib/groupguard.py` refuse tout autre groupe (`evidence/09`) | ✅ |
| Audio sur le groupe + vidéo **et** audio sur la TV | la TV joue YouTube nativement (son inclus) ; le relais ne fait que la lire ; le groupe reçoit l'audio | ✅ |
| Comportement dégradé pendant les pubs accepté | aucune pub rencontrée en ~30 casts (les sessions créées par le banc n'en ont pas) ; le relais traite tout changement d'id comme une nouvelle vidéo (arrêt puis recalage) — mécanisme testé par changement de vidéo | ⚠️ accepté, **non observé** |

## 3. Les trois pistes indépendantes + relecture croisée

| Piste | Preuves propres | Verdict |
|---|---|---|
| 1. Protocole Cast / appareil virtuel | `evidence/11`, `12` ; `notes/track1-protocol.md` | réfutée, avec mesure |
| 2. Extraction, split, stratégie de sync | `evidence/06`, `07`, `08`, `13`, `17`, `19`, `21`, `22`, `24`, `26`, `29`, `33`, `34`, `35` | voie retenue, mesurée |
| 3. Pont matériel ≤ 100 $ CA | `notes/track3-hardware.md` (recherche sourcée, aucun matériel) | sans objet, 3 blocages documentés |
| Relecture croisée | `notes/cross-review.md` (389 lignes, chiffres re-dérivés) | ✅ a corrigé 8 affirmations |
| Consensus écrit | `notes/consensus.md` (recommandation, désaccords conservés, décision motivée) | ✅ |

## 4. Le critère de mesure

| Exigence | Preuve | Verdict |
|---|---|---|
| Protocole de mesure **reproductible et documenté** | `notes/measurement-protocol.md` ; instrument validé sur offsets connus (`evidence/14`, biais +6 ms) | ✅ |
| **Offset A/V réel** mesuré, pas une impression | enregistrement du micro du téléphone, bips 350 Hz (TV) vs clics 2310 Hz (cuisine), analyse par bandes | ✅ |
| **≤ 150 ms** | −20 / −20 / −20 ms dans une vidéo ; max 34 ms sur la session | ✅ |
| **Au moins trois moments** d'une **session ≥ 10 min** | 5 moments (0.75 / 4 / 7.25 / 10.5 / 13.75 min) d'une session continue de **911 s** (`notes/00-labo.md` A30) | ✅ |
| **Sans tendance de dérive** | dérive **0.0 ± 0.0 ms/10 min** à l'intérieur d'une vidéo ; une marche de +28 ms au changement de vidéo (pipeline différent), pas une dérive | ✅ (avec la réserve documentée) |

## 5. Comportements de transport

Testés **contre le service déployé** (`evidence/35-transport-matrix.json`, 11/11) :
lecture, la TV joue la même vidéo, pause, reprise, seek +7 min, seek −6 min, volume TV → groupe
(à la baisse puis à la hausse, en lecture), arrêt, nouvelle session, nouvelle vidéo côté TV.
Enchaînement automatique vérifié séparément sur le même service (`evidence/36`).

| Comportement | Côté TV | Côté Nest (groupe) |
|---|---|---|
| lecture | ✅ | rejoint en ~20 s (téléchargement inclus) |
| pause | ✅ | pause en 6.3 s |
| reprise | ✅ | reprend, erreur numérique −67 ms |
| seek ± | ✅ | suit (35 s de silence lors d'un saut de 7 min : **rebuffer YouTube**, pas le relais — journal `tv_waiting`) |
| volume | ✅ | **suit le volume TV** (0.22 → 0.22, 0.35 → 0.35) |
| arrêt | ✅ | s'arrête en 6.1 s |
| fin de session | ✅ | cuisine arrêtée |
| nouvelle session | ✅ | rejoint en ~20 s |
| enchaînement automatique | ✅ | suit la vidéo suivante (`evidence/36`) |

## 6. Rollback

`notes/rollback.md`, testé : silence en **11.2 s**, volumes **identiques** avant/après
(`evidence/28-rollback-test.json`). Rollback du conteneur documenté (niveau 3, destructif :
seulement avec accord explicite).

## 7. Contraintes de conduite

| Contrainte | État |
|---|---|
| Aucune affirmation sans preuve LAN | 36 fichiers de preuve ; 2 affirmations retirées faute de preuve (génération de la TV, MAC/service de `.2`) |
| Hypothèse / mesure / conclusion à chaque tentative | `notes/00-labo.md` A1–A32 |
| Ne jamais répéter sans variable nouvelle | documenté : 3 essais de récepteur virtuel avec variable (cert, journalisation, pair), 4 politiques d'atterrissage successives |
| Changements réversibles | volumes restaurés, sessions arrêtées, `state/lead_adj.json` supprimable, service `disable` possible |
| Aucun secret affiché | ⚠️ **un jeton GitHub a été lu dans la config OpenCode et affiché une fois** (2026-10-25, pendant la publication) ; propriétaire invité à le révoquer. Le dépôt public ne contient aucun secret (vérifié) |
| Aucune suppression définitive | fichiers déplacés, jamais supprimés ; `evidence/11` régénéré après correction d'un bug de journalisation, l'original conservé en `11a` |
| Achat matériel ≤ 100 $ CA, après consensus | **aucun achat** ; `notes/track3-hardware.md` conclut qu'aucun matériel ≤ 100 $ ne peut corriger la voie Nest |

## Conclusion

Les critères du PoC sont **atteints** : offset perçu mesuré à 5 moments d'une session de 911 s
(max 34 ms, dérive nulle dans une vidéo), comportements de transport complets côté TV et côté
Nest, rollback écrit et testé, trois pistes documentées avec leurs preuves, relecture croisée et
consensus écrits.

Ce qui **n'est pas** prouvé et doit rester dit : les publicités (jamais rencontrées), la
stabilité de `delta` selon le format du contenu et le mode image de la TV, et la mesure
acoustique sur du contenu **arbitraire** (elle exige un marqueur ; deux méthodes essayées et
documentées comme non concluantes, `notes/00-labo.md` A29).
