# Guida per il nuovo utente

Guida semplice per chi non conosce Telegram e vuole iniziare a ricevere le
letture dei sensori e gli allarmi dal bot. Esempio: **Maria Rossi**, appena
iscritta a Telegram.

_English version: [USER-GUIDE.md](USER-GUIDE.md)._

---

## In due parole

Il bot vive dentro **Telegram** (l'app di messaggistica). Ti manda i valori
dei sensori e gli avvisi (allarmi, blackout) come **messaggi privati**. Prima
di poterli ricevere devi fare due cose: farti **abilitare** da un
amministratore e **attivare** la chat privata col bot.

---

## Passo 1 — Installa Telegram e crea l'account

1. Scarica **Telegram** dallo store del telefono (o vai su https://telegram.org).
2. Registrati con il tuo numero di telefono e scegli un nome utente (es. *Maria Rossi*).

## Passo 2 — Raggiungi il bot

Chiedi all'amministratore:
- il **link del gruppo** Telegram del progetto, **oppure**
- il **link diretto del bot** (qualcosa tipo `t.me/NomeDelBot`).

Aprilo e toccalo per entrare. Non serve capire altro per ora.

## Passo 3 — Scopri il tuo codice utente (ID) e dallo all'amministratore

Il bot ti fa vedere i dati solo se l'amministratore ti ha **abilitato**. Per
abilitarti gli serve il tuo **ID** (un numero).

1. Scrivi al bot (nel gruppo o in privato) il comando:

   ```
   /myid
   ```

2. Il bot risponde con una riga tipo: `Your Telegram ID: 123456789`.
3. **Copia quel numero e invialo all'amministratore** (per messaggio, email, come preferisci).
4. Aspetta la sua conferma: ti aggiunge alle sue liste e abilita l'accesso ai sensori che ti competono.

> Senza questo passo il bot non ti mostrerà alcun dato, anche se lo avvii.

## Passo 4 — Attiva i messaggi privati col bot

Le risposte e gli avvisi arrivano **in chat privata**, quindi va attivata una volta.

**Modo A (dal gruppo):**
1. Scrivi un comando qualsiasi nel gruppo, per esempio `/list`.
2. Il bot pubblica un messaggio con un pulsante **"Avvia bot"**.
3. Toccalo: si apre la chat privata del bot.
4. Dentro quella chat premi **Start** (o **Avvia**) in basso.

**Modo B (diretto):**
1. Apri la chat del bot.
2. Premi **Start** in basso, oppure scrivi `/start`.

In entrambi i casi il bot risponde **"Bot activated"** (o *Registration complete*): sei attivo.

## Passo 5 — Iscriviti agli avvisi che ti interessano

Ora usa i comandi **nella chat privata col bot**:

| Comando | Cosa fa |
|---|---|
| `/list` | Mostra tutto quello che puoi vedere: sensori con i valori, e in fondo i **gruppi blackout** disponibili |
| `/get` | I valori attuali dei tuoi sensori |
| `/digest <nome> on` | Ti **iscrivi**: riceverai il riepilogo giornaliero di quel sensore. Es. `/digest SM2_UTA1_T on` |
| `/digest <id_blackout> on` | Ti iscrivi agli **avvisi di blackout** di un gruppo. Es. `/digest R2 on` (gli id li vedi in fondo a `/list`) |
| `/digest` | Mostra a cosa sei già iscritto |
| `/graph <nome>` | Ti manda un grafico dell'andamento |
| `/silent <nome> <ore>h` | Silenzia gli avvisi di un sensore per N ore (es. `/silent SM2_UTA1_T 8h`) |
| `/help` | Elenco di tutti i comandi |

Il nome di un sensore è `dispositivo_campo`, per esempio `SM2_UTA1_T`
(dispositivo `SM2_UTA1`, campo temperatura `T`). Non serve rispettare
maiuscole/minuscole.

---

## Cose utili da sapere

- **Le risposte arrivano in privato e senza suono.** Non aspettarti una
  notifica sonora per ogni risposta: apri la chat del bot per vederle.
- **Non vedi nessun dato?** Probabilmente l'amministratore non ti ha ancora
  abilitato (torna al Passo 3) — l'accesso ai sensori è deciso da lui.
- **Iscrizioni:** all'inizio non sei iscritto a nulla. Il riepilogo giornaliero
  e gli avvisi di blackout arrivano **solo** ai sensori/gruppi a cui ti sei
  iscritto con `/digest ... on`.
- **Usi Telegram Web?** A volte la pagina non si aggiorna da sola: se un
  comando resta con una sola spunta e non vedi la risposta, ricarica con
  **Cmd/Ctrl+R** — è un limite del browser, non del bot.

Per l'elenco completo dei comandi e i dettagli tecnici vedi il
[README](../README.md).
