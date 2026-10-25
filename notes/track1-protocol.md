# Piste 1 — Protocole Cast et apparition de l'appareil virtuel

Objectif : le conteneur peut-il se présenter comme **cible de cast supplémentaire** dans l'app
YouTube du téléphone ? C'était l'hypothèse principale du projet.

Preuves : `evidence/11-virtual-receiver.log` (run corrigé, 02:00),
`evidence/11a-virtual-receiver.log` (run d'origine, conservé tel qu'audité),
`evidence/12-virtual-receiver-protocol.json`.
Code : `probe/virtual_receiver.py`, `lib/castv2.py`, `tools/vrctl.sh`.

> **Ce que cette piste ne prouve pas, et qu'elle prétendait prouver.** La première version
> concluait « l'expéditeur abandonne donc le certificat est refusé » en s'appuyant sur
> `sender_disconnected = 0`. C'était un **artefact de journal** : une fermeture propre sortait
> par `break` sans rien écrire. Le compteur ne distinguait pas « abandon » de « fermeture
> propre ». Corrigé, le journal montre maintenant 12 fermetures **propres**. Le raisonnement
> a été refait en conséquence.

---

## 1. Ce qui a été construit, et ce que ça démontre vraiment

| Brique | Implémentation | Démontré par |
|---|---|---|
| Annonce mDNS | service `_googlecast._tcp`, clés TXT réelles (`id fn md ve ca rs st ic rm bs nf`) | `11` : `mdns_advertised` |
| Transport | TLS 8009, certificat auto-signé `CN=VirtualCast-PoC` | `11` : `tls_established` (TLS 1.3) |
| Protocole | `CastMessage` protobuf, préfixe longueur 4 octets, namespaces réels | `11` : messages décodés d'un pair réel |
| Récepteur applicatif | « Default Media Receiver » : `LAUNCH/LOAD/PLAY/PAUSE/SEEK/SET_PLAYBACK_RATE/GET_STATUS` | `12` — **mais voir la réserve ci-dessous** |
| Authentification | `DeviceAuthMessage` : lecture du challenge, signature du `sender_nonce`, `AUTH_REPLY` | `11` : `deviceauth_message` → `deviceauth_reply_sent` |
| Découverte réelle | un expéditeur Google du LAN trouve l'appareil **sans qu'on lui dise** | `11` : 12 connexions spontanées de `192.168.2.2` |

**Réserve qui change tout sur `12`** : ce fichier vient de `test_virtual_receiver.py`, c'est-à-dire
**notre client contre notre serveur** (`"host": "127.0.0.1"`). Il valide notre sérialisation
protobuf, **pas** la conformité face à un expéditeur tiers. Et le « ratio média/mur 1.0002 » est
**circulaire** : la tête de lecture est calculée par notre propre récepteur. Ces deux chiffres
ne doivent pas être présentés comme des mesures.

## 2. Le point de blocage, mesuré

Un **véritable expéditeur Google** présent sur le réseau attaque le récepteur tout seul :
`192.168.2.2` (poste ASUSTek ; il sert `llama.cpp` sur :8080, ce qui indique une pile Cast de
Chrome — **constat de session, non archivé dans `evidence/`**, à ne pas citer comme preuve).
Un second poste, `192.168.2.115` (le portable du propriétaire), a fait de même lors du premier
run. Les deux comportements sont identiques.

Séquence, répétée 12 fois en 55 s (`evidence/11-virtual-receiver.log`) :

```
tls_established          TLS 1.3
deviceauth_message       kind=challenge  sender_nonce=…  signature_algorithm=1
deviceauth_reply_sent    certificat auto-signé, signature 256 octets
sender_closed_cleanly    <- fermeture propre, immédiate
```

Comptage exact : `CONNECT` = **0**, `LAUNCH` = **0**, toute autre message = **0**. L'expéditeur
n'envoie jamais rien d'autre que le challenge d'authentification.

## 3. Verdict, et les quatre causes concurrentes

**L'hypothèse principale est réfutée dans les faits** (on ne peut pas recevoir le cast de l'app
YouTube), mais **la mesure ne dit pas *pourquoi***. Quatre causes restent compatibles avec le
silence observé, et la note d'origine n'aurait pas dû en écarter une par avance :

1. **Chaîne de certificats non signée par Google** (l'explication la plus probable, et un fait
   connu du protocole : la clé privée est dans un appareil certifié).
2. **Réponse malformée pour un parseur Google.** Notre self-check (signature vérifiée par notre
   propre clé, protobuf relu par notre propre parseur) ne dit rien des attentes d'un tiers.
   Trois défauts plausibles dans le code : `intermediate_certificate` et `crl` vides ;
   `hash_algorithm` de la réponse codé en dur à SHA256 alors que le challenge n'annonce pas le
   sien ; signature couvrant seulement `sender_nonce` (peut-être `nonce‖nonce` attendu).
3. **Poignée de main incomplète.** Le récepteur ne renvoie jamais de challenge vers
   l'expéditeur (`if which != "challenge": return` jette sa réponse). Si l'authentification est
   mutuelle, l'expéditeur attend un challenge qui n'arrive jamais. **C'est le trou le plus
   sérieux et le moins coûteux à fermer.**
4. **Le pair n'est pas un expéditeur complet** (simple sonde de découverte).

**Raison plus large, qui rend le débat presque académique** : même avec un certificat valide, un
récepteur maison devrait encore obtenir le **binaire du récepteur YouTube** (`233637DE`), qui
n'est pas distribué publiquement. Le certificat est **nécessaire mais pas suffisant**.

**Conclusion opérationnelle** : on n'engage pas de travail supplémentaire sur cette piste tant
que le test décisif n'est pas fait — il est bon marché et il n'a pas été fait :

> ouvrir `chrome://media-router-internals` dans Chrome sur `192.168.2.115`, cliquer sur
> « Relais Cast PoC », et lire le **motif d'échec en clair** (certificat / parse / timeout).

Tant que ce n'est pas lu, le dossier ne peut pas affirmer « c'est le certificat ».

## 4. Ce qui reste ouvert, à faire en 1 minute

- Le nom **« Relais Cast PoC » apparaît-il dans la liste de cast du téléphone ?** `.2` l'a
  découvert tout seul (indice fort), mais la confirmation visuelle n'a jamais été faite.
- Il disparaît immédiatement à l'arrêt du processus : vérifiable dans la même minute.

## 5. Ce que cette piste a appris et qui sert au reste

- Un `CastMessage` binaire (`payload_binary`) porte l'auth : un décodeur JSON naïf ne voit rien.
- Les appareils Cast limitent les connexions : en rafale, ils répondent en clair
  (`SSLError: WRONG_VERSION_NUMBER`) → retry avec backoff dans `lib/castv2.py`.
- **Une SSL\* ne doit pas être utilisée par deux threads.** La première version du client
  (thread lecteur + écriture dans le thread appelant) corrompait la couche record
  (`SSL: RECORD_LAYER_FAILURE`). Le client est désormais strictement mono-thread
  (`select()` → `recv()` → `send()`), ce qui est aussi meilleur pour horodater les mesures.
- Deux groupes d'aide Google se contredisent sur la présence d'un Chromecast vidéo dans un
  groupe d'enceintes ; **sur ce LAN, la TV est membre de trois groupes** (`evidence/09`).
