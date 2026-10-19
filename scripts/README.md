# Script di Test e Demo per la Presentazione di Laurea

Questa directory raccoglie gli script di collaudo e dimostrazione end-to-end utilizzati per la validazione della tesi di laurea su **BlueAgent**:

- `test_pipeline.py`: Esegue l'intero ciclo di vita (provisioning del team, generazione chiavi WireGuard, configurazione del namespace di rete del giocatore, deploy del container e verifica automatizzata).
- `test_complete_ssh_flow.py`: Dimostra l'accesso interattivo del partecipante (connessione SSH attraverso il tunnel WireGuard all'interno del container di challenge `ubuntu-ssh`).
- `test_ctfd_flow.py`: Testa l'integrazione del plugin BlueAgent con CTFd (login concorrenti, recupero sfide e sottomissione dei verdetti).
