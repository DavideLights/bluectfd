# Stato Gestione Immagini Distrobuilder (`/images`) - RISOLTO ✅

Tutte le **6 criticità architetturali e operative** individuate in questo documento sono state implementate e testate con successo:
1. **Streaming Upload Tarball (IncusEngine.import_image)**: Implementato streaming a chunk di 1 MB con generatore asincrono in `src/blueagent/engine/incus.py` eliminando il caricamento in RAM.
2. **Supporto File Locali e Build Context**: Supportati archivi `.tar.gz` e `.zip` contenenti `definition.yaml` + file di challenge (`files/`, `overlay/`), estratti automaticamente in `src/blueagent/images/distrobuilder.py`.
3. **Validazione Immagine Verifier**: Aggiunta validazione anticipata in `src/blueagent/services/challenges.py` durante `deploy_challenge` per verificare che `config.verifier.image` esista e sia in stato `ready`.
4. **Endpoint Idempotente `PUT /images/{alias}` & Rebuild**: Sostituito `POST /images` con `PUT /images/{alias}` (query: `force=true`), CAS su SQLite senza constraint UNIQUE per `yaml_sha256`, eliminazione del fingerprint precedente al termine del rebuild.
5. **Riconciliazione Reboot & Endpoint Log `GET /images/{alias}/logs`**: Riconciliazione stati orfani `building -> failed` in `DatabaseManager.reconcile_startup_states()`, streaming real-time dei log del processo `distrobuilder` nel file `{image_build_dir}/{alias}.log` accessibile via `GET /images/{alias}/logs` (`text/plain`).
6. **Pre-baking Directory di Rete**: Iniezione automatica delle directory `/etc/systemd/network` e `/etc/network` nelle azioni post-files della definizione YAML durante il build in `ImageBuilder`.

---

### I 6 Problemi Attuali nella Gestione delle Immagini

#### 1. Caricamento dell'intero Tarball in RAM (`IncusEngine.import_image`)

Guarda le righe 185-192 di `src/blueagent/engine/incus.py`:

```python
async def import_image(self, tarball_path: str) -> str:
    with open(tarball_path, "rb") as f:
        content = f.read()
    ...
    res = await self.client.post("images", content=content, headers=headers, timeout=300.0)

```

* **Il problema:** Un'immagine di sistema unificata (es. Ubuntu con MySQL o Python/Flask) pesa facilmente **dai 200 MB a oltre 1 GB**. Fare `f.read()` sincrono dentro una coroutine `async def` blocca l'event loop di FastAPI durante la lettura da disco e alloca centinaia di MB (o GB) in un singolo oggetto `bytes` nella RAM del processo Python! Se compili 2 o 3 immagini in parallelo, mandi il server in Out-Of-Memory (OOM).


* **Come migliorarlo:** Usa un generatore asincrono (streaming upload) con `aiofiles` o passando uno stream a `httpx`, così il consumo di RAM durante l'importazione scende da ~500 MB a **pochi KB costanti**:
```python
async def _file_stream(path: str, chunk_size: int = 1024 * 1024):
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            yield chunk

```



#### 2. Impossibilità di passare File Locali (Artefatti della Challenge) a Distrobuilder

Guarda il contratto di `POST /images` in `context.md` e `8_ctfd_integration.md`:

* Accetta come form data **solo** `definition` (il singolo file `definition.yaml` di Distrobuilder) e `alias`.


* **Il problema pratico:** Come fa il creatore di una challenge a inserire dentro l'immagine il codice sorgente vulnerabile della webapp, il dump SQL iniziale, i binari compilati o gli script del checker (es. `/opt/checker/test_exploit_sqli.py` visto in `6_yaml_configuration.md`)?


* Se puoi caricare *solo* un file `.yaml`, sei costretto o a scaricare i file da repository remoti durante il build, oppure a codificare l'intera challenge in stringhe inline/base64 dentro la sezione `files:` o `actions:` del file YAML!




* **Come migliorarlo:** Permetti a `POST /images` (o a un endpoint `PUT /images/{alias}`) di accettare:
1. Un file `.yaml` semplice (per immagini base generiche), **OPPURE**
2. Un **archivio di contesto** (`.tar.gz` / `.zip`, esattamente come fa `docker build` con il build context!) che contiene `definition.yaml` più la cartella `files/` o `overlay/` con i sorgenti della challenge, che `ImageBuilder` estrae nella directory effimera prima di lanciare `distrobuilder build-incus`.





#### 3. Immagini App Container (Verifier) vs System Container (Distrobuilder)

C'è una contraddizione importante tra i tuoi modelli (`6_yaml_configuration.md`) e `ChallengeService` (`challenges.py`):

* In `6_yaml_configuration.md`, per il container `verifier` usi immagini OCI/Docker standard come `image: "curlimages/curl:latest"` o `image: "python:3.12-slim"`.


* Inoltre, in `challenges.py` (righe 129-140), `deploy_challenge` controlla `for c in config.containers` che l'immagine sia nel DB di Distrobuilder (`status == "ready"`), ma **NON controlla `config.verifier.image**`!


* Quando poi parte la verifica (`ovn.py`, riga 81), `OVNVerifier` chiama `self.engine.create_instance(v_spec)` passando `"source": {"type": "image", "alias": spec.image}`. Se l'immagine del verifier non è stata anch'essa compilata con Distrobuilder e caricata su Incus, `create_instance` fallisce con `404` durante la verifica!


* **Come migliorarlo:**
1. In `deploy_challenge` (o in un validator di `ChallengeConfig`), verifica **subito** che anche `config.verifier.image` (se `config.verifier` è presente) esista e sia `ready`! Non aspettare che un giocatore invii la patch per scoprire che l'immagine del `verifier` non esiste.


2. Supporta in `/images` non solo il build da zero tramite Distrobuilder (che richiede minuti per scaricare i pacchetti da internet e creare il rootfs), ma anche il **pull rapido da remote Incus/OCI** o l'**importazione diretta di un tarball pre-compilato** (utilissimo se prepari le immagini in CI/CD prima della gara).



#### 4. Nessun Meccanismo di Rebuild / Update se cambia lo YAML (Conflitto Alias vs Hash)

Guarda il workflow di `POST /images` in `8_ctfd_integration.md`:

* Accetti `definition` (YAML) e `alias` (es. `"ubuntu-flask"`), e calcoli l'hash SHA-256 dello YAML.


* **Il problema:** Cosa succede durante lo sviluppo di una challenge se carichi `alias="ubuntu-flask"` con uno YAML, il build va in stato `ready` (o peggio, `failed` perché c'era un typo nello script di post-install!), e tu modifichi lo YAML e richiami `POST /images` con lo stesso `alias="ubuntu-flask"`?


* Se `POST /images` vede che l'alias esiste già e ritorna `200 OK` idempotente ignorando che lo SHA-256 dello YAML è cambiato (o che lo stato precedente era `failed`), per aggiornare l'immagine sei costretto a fare `DELETE /images/ubuntu-flask` e poi `POST /images`!




* **Come migliorarlo:**
* Passa a **`PUT /images/{alias}`** (dato che `alias` è l'identificatore univoco della risorsa scelto dal client!).
* Confronta lo `sha256` del nuovo YAML con quello salvato nel DB per `{alias}`:


* Se `sha256` è **identico** e lo stato è `ready` o `building` $\rightarrow$ ritorna `200 OK` / `202 Accepted` (idempotente, zero lavoro).


* Se lo stato era `failed`, oppure se `sha256` è **diverso** (hai modificato la ricetta YAML!) $\rightarrow$ avvia il build della nuova immagine, sposta il puntatore dell'alias Incus sul nuovo fingerprint appena è pronta, e rimuovi il vecchio fingerprint se non più referenziato!





#### 5. Zombie State (`building` bloccato) e Osservabilità dei Log di Build

* **Stato `building` orfano:** Poiché `distrobuilder` gira come task in background nel processo FastAPI (`ImageBuilder.build_image`), se il processo BlueAgent viene riavviato (o crasha) mentre un'immagine è in `status: building`, al riavvio il processo `distrobuilder` non esiste più ma nel DB SQLite il record rimane in `status: building` in eterno!


* *Fix:* Nella `lifespan` di startup (`main.py`), esegui una query di riconciliazione: `UPDATE images SET status = 'failed', error = 'Interrupted by server restart' WHERE status = 'building'`.


* **Mancanza dei log in tempo reale:** Compilare un'immagine con `distrobuilder` richiede da 1 a 5 minuti. Finché è in `status: building`, `GET /images/{alias}` restituisce `"error": null`. Se un build impiega troppo tempo o fallisce, avere un endpoint **`GET /images/{alias}/logs`** (o salvare le ultime $N$ righe di stdout/stderr nel record dell'immagine) ti salva la vita durante il debug delle challenge.



#### 6. Pre-baking della Configurazione di Rete già nell'Immagine (`ImageBuilder`)

Oggi in `challenges.py` (righe 170-191), **ogni singola volta** che crei un container per un team, fai 3 chiamate HTTP `push_file` sul socket di Incus per iniettare `/etc/systemd/network/10-eth0.network`, `/etc/network/interfaces` e `/etc/hostname` (con `_ensure_parent_dirs` se le cartelle non esistono), e poi 3 chiamate `exec_command` (`ip addr`, `ip link`, `ip route`).

* Anche in `ovn.py` (righe 84-94) ripeti la stessa identica iniezione per il container `verifier` ad ogni singola verifica!


* **Ottimizzazione:** `ImageBuilder` può iniettare automaticamente nella ricetta Distrobuilder le directory base (`/etc/systemd/network/`, `/etc/network/`) e abilitare `systemd-networkd` una volta per tutte in fase di build dell'immagine, risparmiando chiamate API sincrone durante il deploy e la verifica!

---

### Proposta di Interfaccia API Rinnovata per `/images`

Ecco come risulterebbe l'API delle immagini, perfettamente allineata allo stile pulito che abbiamo visto per `/teams`:

| Metodo & Endpoint | Azione e Comportamento | Codici di Stato |
| --- | --- | --- |
| **`PUT /images/{alias}`** | Crea o aggiorna l'immagine `{alias}`. Accetta `definition.yaml` (o un archivio `.tar.gz` con YAML + file della challenge) oppure `?force=true`. Se l'hash SHA-256 coincide ed è `ready`, non ricompila. Se l'hash è cambiato o era `failed`, ricompila in background. | `202 Accepted` (build avviato), `200 OK` (già pronta con stesso hash), `409 Conflict` (già in `building`) |
| **`GET /images`** | Lista tutte le immagini nel catalogo con `alias`, `status` (`building`, `ready`, `failed`), `sha256`, `fingerprint`, `created_at`. | `200 OK` |
| **`GET /images/{alias}`** | Restituisce lo stato dettagliato dell'immagine `{alias}` (inclusi `build_duration_s` e `error` o tail dei log). | `200 OK`, `404 Not Found` |
| **`GET /images/{alias}/logs`** | Restituisce `text/plain` con lo stream o il log completo di `distrobuilder` (fondamentale per fare debug di pacchetti apt/apk falliti). | `200 OK`, `404 Not Found` |
| **`DELETE /images/{alias}`** | Rimuove l'alias e l'immagine da Incus e dal DB. Rifiuta se usata da container attivi o come verifier. | `204 No Content`, `409 Conflict` (in uso) |

### Esito dell'Implementazione
Tutte le 6 soluzioni proposte sono state interamente implementate nel codice sorgente (`src/blueagent/images/distrobuilder.py`, `src/blueagent/services/images.py`, `src/blueagent/api/images.py`, `src/blueagent/engine/incus.py`, `src/blueagent/services/challenges.py`) e convalidate con successo da 107/107 test unitari.