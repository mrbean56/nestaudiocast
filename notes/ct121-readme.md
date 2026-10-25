# CT 121 `castrelay` — relais Cast cuisine <-> TV (PoC)

IP 192.168.2.21 · Debian 13 · 2 vCPU / 1 GiB / 6 GiB (local-lvm) · non privilégié · `onboot=1`
Créé le 2026-10-25. Ajouté à la sauvegarde quotidienne de 03:45 (`backup-7fe8a9f9-0d04`).

> **État réel au 2026-10-25 13:30** : groupe **« Cuisine »** créé (5 Nest Audio, aucun appareil
> vidéo) ; le relais suit la TV et tient l'erreur **numérique** sous ~30 ms, dérive ≤ 4 ms/10 min
> (`evidence/29-*`). **Aucune mesure acoustique de l'offset perçu n'existe encore** : elle demande
> le microphone du téléphone en débogage sans fil (`tools/acoustic_session.py IP:PORT`). Voir
> `notes/consensus.md` et `notes/00-labo.md`.

## Rôle

Suivre en **lecture seule** la session YouTube de la TV du salon (`Chromecast Salon`, 192.168.2.80),
récupérer l'audio de la vidéo en cours (`yt-dlp`) et le jouer **synchronisé** sur le groupe
d'enceintes de la cuisine (5 Nest Audio), avec un offset A/V mesuré ≤ 150 ms.

Rien n'est jamais envoyé à la TV : elle garde son interface YouTube native et son propre son.

## Emplacements

| Chemin | Contenu |
|---|---|
| `/opt/castrelay` | code complet du PoC (dépôt de travail : `~/opencode/nestaudiocast` sur CT 118) |
| `/opt/castrelay/notes/00-labo.md` | cahier de laboratoire : hypothèse / mesure / conclusion par tentative |
| `/opt/castrelay/notes/measurement-protocol.md` | protocole de mesure de l'offset A/V |
| `/opt/castrelay/notes/rollback.md` | procédure de rollback |
| `/opt/castrelay/evidence/` | toutes les preuves (JSON + journaux) |
| `/etc/castrelay.conf` | `GROUP=Cuisine` (résolu par mDNS à chaque démarrage), `TV`, `DELTA`, `POLL` |
| `/etc/systemd/system/cast-relay.service` | service always-on ; `ExecStopPost` = arrêt d'urgence |
| `/etc/systemd/system/cast-relay.service` | service, **désactivé** tant que `GROUP` est vide |

## Service

```sh
systemctl status cast-relay
systemctl enable --now cast-relay    # always-on : suit toute vidéo YouTube jouée sur la TV
journalctl -u cast-relay -f
```

Le wrapper refuse de démarrer si `GROUP` est vide, et `lib/groupguard.py` refuse tout groupe
contenant autre chose qu'une enceinte audio (les trois groupes d'origine contiennent la TV ;
seul « Cuisine » est autorisé). **Attention** : le service démarre tout seul après un reboot et
suit alors la TV en permanence — c'est le comportement voulu, mais il faut le savoir.

Journaux : un fichier `evidence/relay-<date>.jsonl` par démarrage (~1 Mo/heure au repos). Rien
n'est supprimé automatiquement : les déplacer vers `evidence/archive/` de temps en temps.

## Arrêt d'urgence

```sh
cd /opt/castrelay && .venv/bin/python lib/emergency.py
```

Silencie les 5 Nest Audio et les 3 groupes, arrête les processus du projet, ne touche pas à la TV.
Restauration des volumes : `lib/emergency.py --restore`.

## Dépendances

`python3-venv` avec un venv dans `/opt/castrelay/.venv` (zeroconf, pychromecast, numpy,
cryptography, yt-dlp), plus `ffmpeg` par apt. Aucun accès Internet sortant n'est requis, sauf
`yt-dlp` vers YouTube.

## Ce que ce conteneur ne fait pas

- Il ne se présente pas comme une cible de cast : c'est prouvé impossible sans certificat
  d'appareil signé par Google (`notes/track1-protocol.md`).
- Il ne touche ni Jellyfin, ni Tdarr, ni le tunnel PIA, ni MariaDB (.15), ni nginx (.9).
