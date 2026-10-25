# État du PoC — 2026-10-25 13:50

Une page pour savoir où en est le projet sans lire le cahier de laboratoire.

## En service
| Élément | État |
|---|---|
| CT 121 `castrelay` (192.168.2.21) | **en service**, `onboot=1`, sauvegardé 03:45, `/README.md` à jour |
| Groupe Google Home **« Cuisine »** | créé par le propriétaire, 5 Nest Audio, **aucun appareil vidéo** |
| `relay/cast_relay.py` | suit la TV en lecture seule, alimente le groupe, redémarre seul sa liaison |
| `lib/groupguard.py` | refuse tout groupe contenant autre chose qu'une enceinte (les 3 groupes d'origine contiennent la TV) |
| `lib/emergency.py` | arrêt d'urgence testé : silence en 11,2 s, volumes rendus à l'identique |
| `cast-relay.service` | **désactivé volontairement** : à activer (`systemctl enable --now cast-relay`) après la calibration acoustique |

## Criteres d'acceptation
| Critère | Résultat | Preuve |
|---|---|---|
| offset **numérique** stable, sans dérive sur ≥10 min | **OK** : session de 690 s, +7,7 ms de moyenne, σ 10,3 ms, dérive −5,5 ± 1,2 ms/10 min | `evidence/29-session-report.json` |
| offset **perçu** ≤ 150 ms | **MESURÉ : −30 ms ± 3 ms** avec `DELTA = 0.295` (PASS) ; +295 ms sans calibration (`evidence/audio/31-probe.wav`, `23-acoustic-…-2.wav`). Manque la série 3 mesures sur ≥10 min | `evidence/audio/*`, A28 du cahier |
| lecture / pause / reprise / seek / volumes / arrêt | **OK** sur le matériel | `evidence/21`, `22`, `24`, `26` |
| enchaînement automatique | **OK** (suivi, recalage ~25 s) | `evidence/30-session-report.json` |
| fin de session / nouvelle session | **OK** (cuisine arrêtée puis reprise) | `evidence/22-session-report.json` |
| rollback écrit et testé | **OK** | `notes/rollback.md`, `evidence/28-rollback-test.json` |

## Pour finir (une seule action)
Réactiver le **débogage sans fil** sur le téléphone, puis :
```sh
# le téléphone posé où il entend la TV ET la cuisine
tools/acoustic_session.py <IP:PORT>        # 3 enregistrements de 60 s à 1, 5 et 10 min
```
→ 3 fichiers `evidence/23-acoustic-*.json`. Si l'offset médian dépasse 150 ms, régler
`DELTA` dans `/etc/castrelay.conf` avec `delta = +OBS_0` puis refaire une passe
(`notes/measurement-protocol.md` §4, contrôle de linéarité inclus).

## Ce qui n'est pas prouvé
- L'offset **perçu** (la seule grandeur qui compte pour l'oreille) : tout le reste est numérique.
- La stabilité de `delta` entre sessions et entre formats de contenu (mode image de la TV,
  24p/30p, HDR) : aucun test.
- Le comportement pendant les **publicités** : aucune pub rencontrée lors des essais.
- Le rollback au volume normal (l'essai était à 0,05 / 0,10).
