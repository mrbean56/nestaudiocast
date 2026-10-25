# Protocole de mesure de l'offset A/V (reproductible)

But : produire un **chiffre** d'offset audio/vidéo perçu, répétable, avec une incertitude,
et une **tendance** (absence de dérive) sur une session ≥ 10 min. Ce protocole remplace
toute impression d'écoute.

Instrument : `measure/analyze_offset.py`. Il est testé contre des enregistrements
synthétiques à offset **connu**, y compris les cas piégeux (repliement, distorsion de
l'enceinte du téléviseur, lit musical dans les bandes de mesure) : `evidence/14-analyzer-selftest.json`.

> **Ce protocole n'a encore jamais été exécuté sur du matériel réel.** Tout ce qui suit est
> prêt, mais aucune valeur d'offset perçu n'existe à ce jour. La relecture croisée
> (`notes/cross-review.md`) a d'ailleurs montré que la première version de l'analyseur
> produisait un faux PASS au-delà de 500 ms : c'est corrigé, et le protocole en tient compte.

---

## 0. Les deux grandeurs, et pourquoi il ne faut pas les confondre

| Grandeur | Définition | Comment on l'obtient |
|---|---|---|
| offset **numérique** | écart entre la tête de lecture du groupe et celle de la TV, lues par CASTv2 | en continu, par le relais |
| offset **perçu** (A/V réel) | écart entre le son entendu dans la cuisine et l'image vue à la TV | par enregistrement acoustique |

Seule la deuxième décide du PoC. La première sert à prouver l'absence de dérive et à
piloter la correction — elle **ne voit aucune** des deux latences de rendu.

## 1. Le signe de `delta` (à ne pas se tromper)

Notations :
- `p_g(W)`, `p_tv(W)` : têtes de lecture à l'instant `W` ;
- `L_g`, `L_tv` : latences de rendu (délai entre « la tête atteint la position m » et
  « le son/l'image sort ») ;
- le code maintient `p_g = p_tv + delta`.

Un appareil émet la position média `m` à l'instant `W_dev(m) + L_dev`. Comme
`p_g = p_tv + delta`, pour une même position `m` on a `W_tv = W_g + delta`, donc :

```
E_g  = W_g + L_g = W_tv - delta + L_g
E_tv = W_tv + L_tv
offset perçu  O = E_g - E_tv = (L_g - L_tv) - delta
```

**Pour annuler l'offset : `delta = L_g - L_tv`.**

Et comme `OBS_0` est mesuré avec `delta = 0`, on a `OBS_0 = L_g - L_tv`, donc :

> **`delta = +OBS_0`** (et non `-OBS_0`).

Contrôle par l'intuition : si la cuisine est entendue **en retard** (`OBS_0 > 0`), il faut que
sa tête de lecture soit **en avance** sur celle de la TV, donc `delta > 0`. ✔

Contrôle empirique obligatoire (il rattrape toute erreur de signe) : régler
`delta = OBS_0`, re-mesurer. L'offset doit **diminuer**. S'il double, le signe est à inverser.

## 2. Signal de mesure

- **TV** : une vidéo YouTube de test dont la bande son est un bip ~1 kHz coïncidant avec un
  flash blanc.
- **Cuisine** : le relais en mode mesure (`--measure-tone 2310`), qui joue une suite de clics
  à **2310 Hz** sur la même tête de lecture que la vidéo.

Deux exigences non négociables, toutes deux nées d'échecs mesurés :

1. **Fréquences non harmoniquement liées.** Un bip à 1 kHz reproduit par un haut-parleur de
   TV non linéaire produit de l'énergie à 2 kHz — exactement là où on écouterait le clic
   cuisine, ce qui fabrique un faux « offset 0 ». 2310 Hz n'est pas un harmonique de 1 kHz.
   (`evidence/14`, cas `nonlinear_tv_2nd_harmonic`.)
2. **Même conteneur que la production : M4A.** La latence de démarrage diffère de ~70 ms
   entre WAV et M4A (`evidence/16`) — presque la moitié du budget. Utiliser
   `assets/click_train_2k.m4a` (à régénérer à 2310 Hz, cf. ci-dessous), jamais le WAV.

Commande de génération de la piste de mesure (fréquence non harmonique) :

```sh
.venv/bin/python tools/make_assets.py --click-train 720 2310 assets/click_train_2310.wav
./bin/ffmpeg -y -i assets/click_train_2310.wav -c:a aac -b:a 128k assets/click_train_2310.m4a
```

Puis `--measure-tone 2310 --measure-file assets/click_train_2310.m4a`.

**Phase.** Le clic doit tomber sur la même grille que le bip de la vidéo. Une erreur de phase
est indiscernable d'un offset (période 1 s). Il faut donc vérifier la phase **avant** de
mesurer, en analysant l'audio téléchargé de la vidéo de test (`yt-dlp` l'a déjà) pour trouver
la position réelle des bips, et ajuster `--click-phase`.

## 3. Séquence d'une mesure (≈ 2 min)

1. TV libre, vidéo de test lancée depuis le téléphone. Ne rien changer côté TV.
2. Groupe cuisine **dédié** vérifié par `lib/groupguard.py` — jamais un groupe existant.
3. Enregistreur à une position **notée et repérée** (photo + marque au sol). Même position à
   chaque mesure : c'est la seule erreur systématique du protocole (±0.3 m ≈ ±1 ms).
4. Lancer le relais, attendre `in_sync`.
5. Enregistrer 30 s → **mesure 1**. Noter aussi l'offset numérique du relais sur la même fenêtre.
6. Répéter à **t = 0, 5, 10 min** de la même session (mesures 1, 2, 3).
7. Analyser :
   ```sh
   .venv/bin/python measure/analyze_offset.py rec_01.wav \
       --grp-freq 2310 --period 1.0 --expect-ms <offset numérique> --json evidence/av-01.json
   ```

### Le repliement, et comment le traiter

Avec un clic toutes les 1 s, un offset de `X` est **physiquement indiscernable** de `X − 1000 ms`.
Un premier analyseur rendait `−100 ms` pour un vrai `+900 ms`, avec verdict PASS : un faux
PASS silencieux. Il ne le fait plus — il imprime la **famille d'alias** dans le verdict — mais
la décision reste au protocol :

- donner `--expect-ms` (l'offset numérique, qui est toujours dans la bonne fenêtre grâce à
  l'asservissement) lève l'ambiguïté ;
- **ou** faire le contrôle de linéarité du §4, qui la lève aussi.

## 4. Calibration de `delta`

1. Première mesure avec `--delta 0` → `OBS_0`.
2. Régler `delta = OBS_0 / 1000` (cf. §1).
3. Re-mesurer : l'offset doit être proche de 0.
4. **Contrôle de linéarité** (obligatoire, il vaut à la fois preuve de signe et levée d'alias) :
   `delta = OBS_0 + 100 ms` doit donner un offset mesuré **de +100 ms**. Si l'offset ne suit
   pas, la mesure est dans un alias et il faut revenir à l'étape 2.
5. Figer `delta` dans `/etc/castrelay.conf`.

## 5. Critères d'acceptation

| Critère | Seuil | Preuve attendue |
|---|---|---|
| offset perçu médian | ≤ 150 ms | 3 fichiers `evidence/av-0*.json` |
| dispersion intra-mesure | σ ≤ 40 ms | champ `offset_stdev_ms` (appliqué par le code) |
| dérive | pas de tendance : `|drift_projected_10min|` ≤ 60 ms **et** |valeur| > 2σ | champs `drift_ms_per_min` + `drift_sigma_ms_per_min` |
| linéarité `delta` | pente 1:1 ±10 % sur 2 points | §4.4 |
| transport | lecture, pause, seek, volume, stop, enchaînement auto, fin et reprise de session | journal du relais + constat visuel |
| rollback | procédure écrite et testée | `notes/rollback.md` |

## 6. Limites assumées (à écrire dans le rapport final)

- **La référence est le son de la TV, pas ses pixels.** Tout repose sur « le bip TV est
  solidaire de l'image ». C'est une hypothèse non mesurée sur la synchro interne du téléviseur.
  La seule façon de la lever est une capture optique (240 fps) du flash ; elle n'est pas
  implémentée ici, et `notes/track3-hardware.md` §5.1 la réclame aussi.
- **Un seul point d'écoute pour cinq enceintes.** `onsets()` prend la première arrivée
  au-dessus du seuil, donc l'enceinte la plus proche du micro. C'est « le meilleur cas à cet
  endroit », pas une moyenne de la pièce. Mesurer à 2–3 positions et rapporter la dispersion.
- **`delta` est une constante.** Rien ne garantit sa stabilité : mode image de la TV, format
  du contenu (24p/30p, HDR/SDR), re-négociation du tampon du groupe, charge Wi-Fi (RTT mesurés
  jusqu'à 192 ms, `evidence/07`). Le protocole mesure donc **trois fois** par session, et il
  faut noter à chaque fois le mode image et la résolution du contenu.
- **Le garde-fou « comparer l'offset numérique avant/après substitution » ne teste rien** :
  l'offset numérique ne contient aucune latence de rendu. C'est le §2.2 (même conteneur) qui
  protège, et il ne protège que la partie format, pas la latence de rendu.

## Chaîne acoustique validée hors ligne (2026-10-25 11:45, `evidence/23-acoustic-chain-selftest.json`)
Mélange synthétique : audio réel de `QzomK1fdSUg` (bip TV 350 Hz) + piste cuisine `assets/measure/QzomK1fdSUg.m4a` (2310 Hz) décalée d'un offset connu + bruit. `analyze_offset.py --tv-freq 350 --grp-freq 2310` :
vrai 0 / +40 / −60 / +120 / −140 ms → mesuré +6 / +46 / −54 / +126 / −134 ms, σ ≈ 1 ms, 69–70 paires sur 70 s.
**Biais constant de la chaîne : +6 ms** (formes d'enveloppe bip 75 ms vs clic 30 ms) → à soustraire des mesures réelles.
Exécution réelle : `tools/acoustic_session.py IP:PORT` (téléphone en débogage sans fil, posé où il entend la TV et la cuisine) → 3 enregistrements de 60 s aux minutes 1, 5 et 10 d'une session continue.
