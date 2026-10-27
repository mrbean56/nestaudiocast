# Procédure de rollback

Tout ce que fait le PoC est réversible. Ce document liste les actions dans l'ordre, et dit
précisément ce qui a été testé — et ce qui ne l'a pas été.

**Testé le 2026-10-25 à 01:17** (`evidence/15-panic-test.log`) : arrêt des processus, mute des
5 Nest Audio, vérification qu'aucune session ne tourne. **Ce journal ne contient aucune valeur
de volume** : il ne prouve donc pas la restauration des volumes. Celle-ci a été vérifiée à la
main juste après (retour à 0.0833 / 0.55 / 0.0833 et groupes à 0.2619 / 0.2917 / 0.2417), mais
**sans artefact dédié** — c'est un manque à combler, pas une preuve.

---

## Niveau 0 — « le son s'arrête tout de suite »

```sh
# 1. le relais (CT 121) : service + toute instance lancée à la main
ssh root@192.168.2.21 'systemctl stop cast-relay; pkill -f "^.venv/bin/python -u relay"'
# 2. côté poste de travail : processus locaux, mute des 5 Nest Audio, arrêt des sessions de
#    groupe sans TV (dont « Cuisine »)
cd ~/opencode/nestaudiocast
.venv/bin/python lib/emergency.py
# 3. si la TV joue une vidéo lancée par le banc de test (et seulement dans ce cas) :
.venv/bin/python tools/yt_tv.py stop
```

Le relais s'arrête aussi tout seul proprement : en quittant, il arrête la session du groupe
(`sink.stop()`), pour ne jamais laisser la cuisine jouer derrière lui.

Ordre des opérations :
1. arrête les processus du projet (`logs/relay.pid`, `logs/virtual-receiver.pid`) ;
2. met à **0 et muet les 5 Nest Audio de la cuisine** ;
3. arrête une app de récepteur **sur un groupe, uniquement si le groupe ne contient pas la TV** ;
4. **ne touche à aucun groupe pour le volume.**

> **Correction importante.** La première version muait aussi les 3 groupes. C'est une erreur :
> `SET_VOLUME` sur un groupe **se propage à ses membres**, et tous les groupes d'ici contiennent
> la TV — donc « régler le volume du groupe » revient à changer le volume du téléviseur du
> salon. La garantie est désormais « aucun paquet vers 192.168.2.80 **et** aucun effet de bord
> par un groupe », pas seulement la première moitié.

Vérifier :

```sh
.venv/bin/python lib/silence.py
```

La sortie donne, par point de diffusion : `level`, `muted`, `app`, et `silent`. `silent` vaut
vrai si aucune session ne tourne, ou si le volume est nul/muet. Une ligne avec
`app=Default Media Receiver` **et** `silent=false` est le cas à traiter.

## Niveau 1 — rendre les volumes d'origine

```sh
.venv/bin/python lib/emergency.py --restore
```

Restaure ce que le dernier `silence()` a enregistré (`evidence/silence-state.json`). Si ce
fichier manque, remettre à la main les valeurs relevées à l'inventaire du 2026-10-25 00:33 :

| Point de diffusion | volume d'origine |
|---|---|
| Cuisine Fenêtre Vert (.61) | 0.0833 |
| Cuisine Frigo Orange (.62) | 0.5500 |
| Cuisine Frigo Jaune (.63) | 0.5500 |
| Cuisine Meuble Bleu (.64) | 0.0833 |
| Cuisine Meuble Rose (.65) | 0.0833 |
| Groupe Tous | 0.2619 |
| En Haut | 0.2917 |
| Dehors | 0.2417 |

Restaurer un volume ne fait aucun bruit tant que rien ne joue.

## Niveau 2 — effacer toute trace du PoC sur le réseau

- **Appareil de cast virtuel** : disparaît à l'arrêt du processus (annonce mDNS liée au
  processus). `tools/vrctl.sh stop`. Aucun état persistant.
- **Caches audio** (`assets/yt/`) : déplaçables, jamais supprimés (règle du projet).
- **Journaux et preuves** (`evidence/`) : à conserver, c'est le livrable.

## Niveau 3 — retirer le conteneur dédié

1. Dans le conteneur : `systemctl disable --now cast-relay`.
2. Sur pve (192.168.2.3) : `pct stop 121` puis `pct destroy 121 --purge`
   **(destructif : seulement après accord explicite du propriétaire)**.
3. Retirer le VMID de la sauvegarde :
   `pvesh set /cluster/backup/backup-7fe8a9f9-0d04 --vmid 104,106,107,108,109,110,111,113,116,117,118,119,120,130,140`
   (c'est-à-dire la liste d'origine, sans 121). Pour relire l'état :
   `pvesh get /cluster/backup --output-format json`.
4. DHCP/DNS : rien à faire (IP statique, hors plage DHCP).

## Ce que le projet ne touche jamais

- Stack média : Tdarr (workers GPU à 0), Jellyfin, tunnel PIA, MariaDB (.15), nginx (.9).
- La TV (.80) : **le relais** ne fait que la lire. Le **banc de test** (`tools/yt_tv.py`,
  `tools/scenario.py`) la pilote comme le téléphone (YouTube, volume), uniquement sur
  autorisation du propriétaire (donnée le 2026-10-25 à 10:40 et ~10:47) et en refusant d'agir si
  elle exécute autre chose que Backdrop/YouTube. Les scénarios baissent les volumes TV/cuisine
  pendant l'essai et les **restaurent exactement** (journal `volumes_saved` / `volumes_restored`
  dans `evidence/2x-scenario-*-events.jsonl`). Aucun effet indirect par un groupe.
- Aucun secret affiché ni journalisé.
- Aucune suppression définitive.

## Incident à ne pas répéter

Les 2026-10-25 entre 00:35 et 00:52, **quatre** prises de contrôle de la TV ont eu lieu (tests
`06`, `07`, et les trois groupes démarrés par `probe/groups_seek_rate.py`), parce que **les
trois groupes existants contiennent « Chromecast Salon »**. Une session de groupe remplace
l'app en cours sur la TV. Et comme la TV n'était pas mute (`silence(touch_tv=False)`, jamais
utilisé) alors qu'elle était membre du groupe joué à `level=0.4`, **il est très probable que ces
tests aient aussi fait jouer de l'audio sur l'enceinte du téléviseur.**

Depuis :
- `lib/groupguard.py` refuse toute lecture sur un groupe dont un membre n'est pas une enceinte
  audio (`capabilities == 196612`) ;
- la liste des membres se lit **sans jouer** (`MULTIZONE_STATUS`) : le contrôle ne dérange
  personne et ne coûte rien ;
- `lib/silence.py` arrête désormais **les sessions qu'il a lui-même créées** (un premier test
  avait laissé une session tourner 7 minutes sur `.61` après restauration du volume) ;
- `lib/emergency.py` ne mute plus les groupes ;
- plus aucun test sur un groupe existant : uniquement sur le groupe dédié **« Cuisine »**
  (créé par le propriétaire le 2026-10-25, 5 Nest Audio, aucun appareil vidéo).

Autres leçons du 2026-10-25 :
- `pkill -f motif` lancé dans un `ssh … '…'` **se tue lui-même** (le motif figure dans la ligne de
  commande du shell distant). Toujours ancrer : `pkill -f "^.venv/bin/python -u relay"`.
- Un `adb kill-server` coupe le débogage sans fil du téléphone (port changé) : ne jamais
  relancer le serveur adb pendant une session.

---

## v0.2 — `cast-sender` (CT 121, `/opt/castsender`)

**Non exécuté de bout en bout à ce jour** (outil `tools/rollback_v020.py`, auto-test seulement :
`evidence/48-rollback-v020-selftest.json`). L'ordre ci-dessous est celui que la porte matérielle
exécutera.

```sh
# 1. arrêter la session (groupe arrêté, récepteur sur sa page d'attente, verrou libéré)
python3 -c 'import json,urllib.request as u;print(u.urlopen(u.Request("http://192.168.2.21:7640/api/control",json.dumps({"action":"stop"}).encode(),{"Content-Type":"application/json"})).read())'
# 2. arrêter le service ; son ExecStopPost (`python -m sender.service --cleanup`) arrête la
#    session qu'il a enregistrée et rien d'autre
ssh root@192.168.2.21 systemctl stop cast-sender
ssh root@192.168.2.21 systemctl disable cast-sender
# 3. v0.1 reprend seule le groupe : le verrou absent, le relais suit de nouveau la TV
ssh root@192.168.2.21 'ls /opt/castsender/state/; systemctl is-active cast-relay'
```

- Le verrou `group.owner` est **déplacé**, jamais effacé, s'il reste après un arrêt brutal :
  `mv /opt/castsender/state/group.owner /opt/castsender/state/group.owner.stale-$(date +%s)`
  (le relais le considère périmé après 15 s de toute façon, contrats §7).
- Le Chromecast : `stop` le laisse sur la page d'attente de notre récepteur ; un appui sur
  « Arrêter la diffusion » dans Google Home le rend à Backdrop. Son volume n'est jamais modifié
  par v0.2 (seul le mute sert pendant les bancs, et il est rendu).
- Le relais v0.1 : depuis le 2026-10-27 son `ExecStopPost` passe par `tools/relay-stop-post.sh`,
  qui ne coupe pas le son de la cuisine tant que v0.2 la possède ; sans propriétaire v0.2 il
  lance `lib/emergency.py` comme avant.
