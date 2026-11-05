# Tares-Pilot: Codex und optionale Claude-Crew

Der lokale Controller nutzt vorhandene persönliche Anmeldungen und verbraucht deren
Kontingent. Er bucht keine Dienste, ändert keine Abonnements und verwendet keine
Anbieter-API-Schlüssel. Produktionsfreigabe und automatischer Merge bleiben ausgeschaltet.
Der Pilot läuft unter macOS/Linux. Alle Runtime-Dateien müssen außerhalb **jedes**
Git-Checkouts und des entpackten Factory-Pakets liegen, auch nach Auflösung von Symlinks.

## Voraussetzungen

Python >=3.9 für den Controller, Git, Docker, Node 22.23.3 und eine bereits mit ChatGPT
angemeldete Codex-CLI. Getestet mit Codex **0.166.0-alpha.6**: benötigt werden benannte
Dateisystem-Berechtigungsprofile und `mcpServer/tool/call` im App-Server. Ältere CLIs ohne
diese Fähigkeiten werden nicht auf einen ungeschützten Modus zurückgesetzt. Tares 1.55.1
benötigt Python >=3.11, beispielsweise als getrennte Installation mit uv:

```sh
uv tool install tares==1.55.1 --python 3.13
python3 scripts/tares_runtime.py start --runtime /pfad/factory-runtime --tares /pfad/zur/tares
python3 scripts/tares_runtime.py status --runtime /pfad/factory-runtime
```

Standardports sind API `127.0.0.1:18787` und MCP `127.0.0.1:18788/mcp`. Für eine zweite
Instanz beide Ports mit `--api-port 18797 --mcp-port 18798` festlegen und beim Neustart
wieder angeben. Ports werden im Runtime-Ordner festgehalten. Das lokale Token entsteht mit
Modus 0600, der Runtime-Ordner mit 0700. `status` legt keine Dateien oder Tokens an und
liefert nur dann Exitcode 0, wenn beide identifizierten Prozesse **und** API/MCP gesund sind.
Der Start wartet auf Betriebsbereitschaft und räumt fehlgeschlagene neue Prozesse auf.

## Frische, versionierte Übungen

`examples/tares-pilot` ist die fertige Referenz. Neue Läufe verwenden ausschließlich
`examples/tares-baseline/v1`: Nullwertfehler, fehlende JSON-Option und picocolors 1.1.0.
Das Profil `config/tares-pilot.json` bindet diese Übung an eine Versionsnummer und einen
Manifest-Digest. Die Dateien müssen committed und unverändert sein. Ein gelöstes Ticket
wird schon vor dem ersten Coding-Versuch abgewiesen. Eine veränderte Baseline verlangt
eine neue, geprüfte Version. Gelöste Übungsdateien gehören auf einen wegwerfbaren
Integrationsbranch; sie dürfen die ungelöste Baseline in `main` nicht ersetzen.
Für eine neue Baseline einen neuen Runtime-Ordner und Projektnamen verwenden; historische
Sitzungen mit dem alten Konfigurationsdigest werden nicht umgebunden oder gelöscht.

## Weg A: vorhandene Codex-Anmeldung

`codex login status` muss eine ChatGPT-Anmeldung bestätigen. Die GitHub-App muss für das
Zielrepository freigegeben sein. Eine zusätzliche `gh`-Anmeldung ist nicht nötig.

```sh
python3 tares_adapter.py init --runtime /pfad/factory-runtime --repository dein-konto/dein-projekt
python3 tares_adapter.py run --task PILOT-BUG-001 --runtime /pfad/factory-runtime --repository dein-konto/dein-projekt
python3 tares_adapter.py run --task PILOT-FEATURE-002 --runtime /pfad/factory-runtime --repository dein-konto/dein-projekt
python3 tares_adapter.py run --task PILOT-DEPS-003 --runtime /pfad/factory-runtime --repository dein-konto/dein-projekt
python3 tares_adapter.py status --runtime /pfad/factory-runtime --repository dein-konto/dein-projekt
```

Die Aufgaben laufen seriell in getrennten Worktrees am gepinnten Ausgangscommit. Der
Worker darf nur an den explizit freigegebenen Ticketpfaden schreiben; der Reviewer erhält dort nur Leserechte. Beide nutzen
ein explizites `factory_agent`-Profil: minimale Systemdateien, Projekt und erforderliche
Git-/Node-Werkzeuge sind lesbar; Geschwisterordner und persönliche Anmeldedateien bleiben
außerhalb der Lesefreigabe. Nur der jeweilige temporäre Ordner ist zusätzlich beschreibbar.
Apps, Plugins, globale MCP-Server, Websuche, Projektnetzwerk und Login-Shells sind aus.
Anbieter- und Service-Schlüssel werden nicht weitergereicht. Die persönliche Anmeldung
verwendet nur der CLI-Hauptprozess; sie wird nicht in den Worktree kopiert.

Vor dem Start wird der Versuch dauerhaft reserviert. PID, vollständige Prozessidentität
und Sitzungskennung werden sofort gespeichert. Auch ein Fehler beim Speichern oder
Schreiben des Logs beendet und reappt den gestarteten Prozess. Höchstens zwei Coding-
Versuche und das konfigurierte Zeitbudget sind erlaubt. Ein erneuter `run` setzt dieselbe
Sitzung fort. Anbietergrenzen führen weder zum Zukauf noch zum Wechsel auf API-Abrechnung.

## Ausführung fremden Projektcodes

Der Controller führt **keine** Projekt-Installation, Node-Imports, Tests oder Builds auf
dem Host aus. Vor jeder Installation prüft er Paket- und Lockfile-Daten gegen die beiden
freigegebenen picocolors-Versionen, exakte npm-Registry-URLs und SHA-512-Integritäten.
Weitere Paket-Metadaten und Abhängigkeitsgraphen werden abgewiesen.

Das Werkzeugimage basiert auf einem unveränderlichen Node-Image-Digest. Der Controller
lädt nur die zwei genehmigten Archive über feste HTTPS-Adressen ohne Redirects und prüft
ihre Bytes vor dem Image-Bau. Der Bau läuft ohne Netzwerk. Danach erfolgen `npm ci
--offline --ignore-scripts`, Lint, Tests, Build und unabhängige Abnahme in einem kurzlebigen
Container: kein Netzwerk, keine Host-Zugangsdaten, kein Docker-Socket, nicht-root,
schreibgeschützte Quellen und begrenzte Ressourcen. Nur `node_modules`, `dist` und ein
flüchtiger Container-Tempbereich sind beschreibbar. Container werden auch bei Fehlern
entfernt. npm-Advisories werden als reine Daten für den genehmigten Paketnamen und seine
Version vom festen Registry-Endpunkt abgefragt; HIGH/CRITICAL sperrt den Lauf.

Die CLI-Abnahme prüft **beide** Modi unabhängig von den geänderten Tests: exakte lesbare
Standardausgabe mit `NO_COLOR=1` sowie genau das erwartete JSON-Objekt mit `--json`.
Paketidentität wird zusätzlich vor dieser ausführbaren Abnahme geprüft.

Nach erfolgreicher Prüfung und lesendem Review speichert der Controller einen
Validierungscheckpoint **vor** dem lokalen Commit. Fällt danach Tares aus, bleibt der
Commit gespeichert. Wiederaufnahme überprüft denselben Baum und beendet nur die noch
fehlenden Ledger-Schritte, ohne Coding-/Review-Neustart oder weiteren Versuch.

Nach ausgeschöpftem Coding-Budget ist eine protokollierte Betreuerkorrektur möglich:
`tares_adapter.py verify --task ID --runtime ... --note "Begründung"`. Sie hat ein eigenes
Vier-Minuten-Limit für die vollständige Prüfung und das lesende Review; höchstens zwei
solche Prüfungen sind erlaubt. Versuche und bisherige Fehler bleiben erhalten.

## Deterministische Veröffentlichung

`tasks/<ID>/publication.json` enthält nur die geprüften Dateien samt Git-Blob-Identitäten.
Ein separat vorbereiteter `feat/...`-Integrationsbranch muss denselben Tree wie der lokale
Ausgangscheckout haben. Dessen GitHub-Commit wird explizit gepinnt:

```sh
python3 tares_adapter.py publish --task PILOT-BUG-001 --repository dein-konto/dein-projekt \
  --runtime /pfad/factory-runtime --base-ref feat/pilot-validation --base-sha GITHUB_COMMIT_SHA
```

Der Controller berechnet den kombinierten Zielbaum mit einem separaten Git-Index und
prüft ihn erneut in der isolierten Umgebung. Die Veröffentlichung verwendet anschließend
die vorhandene GitHub-Verbindung über den Codex-App-Server **ohne Modelllauf**. Es gibt
keinen Publisher-Prompt und keinen Zugriff des Agenten auf Connector-Werkzeuge.

Ein fester Plan prüft vor jedem Aufruf Toolname und sämtliche Argumente: genau ein
Repository, erlaubte Dateipfade, gepinnter Tree/Elterncommit, neuer `factory/...`-Branch und
PR gegen den Integrationsbranch. Erlaubt sind nur lesendes `fetch` und
`create_tree → create_commit → create_branch → create_pull_request`. Keine Ref-Updates,
Datei-Einzelupdates, Merges, Kommentare, Releases oder Einstellungen. Ungeplante Aufrufe
werden vor dem Dispatch abgewiesen. Interaktive Freigaben oder Ablehnungen werden nicht
umgangen; sie stoppen den Lauf zur Prüfung durch die bedienende Person.

Vor jeder Mutation und nach der Veröffentlichung wird der Ausgangsbranch erneut gelesen.
Der Ergebniscommit muss exakt den erwarteten Tree und **genau einen** Elterncommit besitzen,
den gepinnten Ausgangscommit. Head, PR-Ziel und PR-Head müssen übereinstimmen.
Ein bewegter Ausgangsbranch stoppt die Transaktion; der neue Stand braucht eine neue
Validierung. Vor dem menschlichen Merge müssen CI und Aktualität nochmals geprüft werden.
Die kostenfreie private GitHub-Konfiguration ersetzt weiterhin keinen Branchschutz.

Jede Transaktion speichert vor einer Mutation ihre Absicht und danach den Connector-Beleg.
Ein unterbrochener Branch-/PR-Aufruf wird anhand des Remote-Zustands wiederaufgenommen;
bestehende fremde Branches werden nie überschrieben, identische PRs nicht doppelt angelegt.
Das Journal bleibt an seinen Plan gebunden. Ein Retry kauft keine Modellnutzung und startet
keinen Publisher-Agenten. Der Tares-PR-Vermerk lässt sich anschließend separat nachholen.

GitHub-CI wird auf GitHub beobachtet. Eine native Tares-GitHub-Synchronisation/Webhook ist
nicht eingerichtet; ein tatsächlicher Merge wird deshalb nicht als automatisch synchronisiert
behauptet. CI enthält keine Anbieteranmeldung und startet keine Coding-Agenten.

## Weg B: Claude-Kollegen mit eigener Berechtigung

Dieser optionale Weg benötigt den eigenen bestehenden Claude-Code-Zugang und die eigene
Repository-Freigabe des Kollegen. Eine Claude-Web-Anmeldung allein genügt nicht. Ohne
Claude-Code-Berechtigung bleibt die native Crew aus; es wird nichts hinzugebucht.

```sh
python3 tares_adapter.py prepare-claude --runtime /pfad/factory-runtime --repository dein-konto/dein-projekt
python3 scripts/start_claude_crew.py --runtime /pfad/factory-runtime --claude /pfad/zur/claude --doctor
python3 scripts/start_claude_crew.py --runtime /pfad/factory-runtime --tares /pfad/zur/tares --claude /pfad/zur/claude
```

`prepare-claude` initialisiert jetzt selbst Projekt, Meilenstein, drei Tickets und Handover
und speichert einen dazu passenden Vorbereitungsbeleg. Der Start-Wrapper verlangt diesen
Beleg, die passenden Tares-Tickets, die persönliche Claude-Anmeldung, **L3-review** und
einen deaktivierten Releaser. `--doctor` prüft Vorbereitung/Anmeldung und startet keine Crew.
Das persönliche Crew-Profil bleibt leer, ein zweiter Code Owner ist nicht verpflichtend.

Die Tares-eigenen Skills steuern die native Crew. Ihre Sessions besitzen eigene
Berechtigungen; Ergebnisse brauchen weiterhin die Factory-Abnahme. Die hier beschriebene
Codex-Sandbox und der deterministische Publisher werden einer nativen Crew nicht automatisch
zugeschrieben. Ein echter Claude-Crew-End-to-End-Lauf wurde mangels persönlicher Berechtigung
nicht durchgeführt; Vorbereitung und Start-Sperren werden getestet.

## Stoppen und Prüfen

```sh
python3 scripts/tares_runtime.py stop --runtime /pfad/factory-runtime
python3 -m unittest discover -s tests -v
FACTORY_DOCKER_TESTS=1 python3 -m unittest discover -s tests -p test_pilot_isolation.py -v
FACTORY_CODEX_TESTS=1 python3 -m unittest discover -s tests -p test_codex_boundaries.py -v
```

Stop signalisiert nur Prozessgruppen, deren Startzeit, vollständige Argumente, Gruppen-ID
und Runtime-Bezug zum gespeicherten Beleg passen. Veraltete oder alte reine PID-Dateien
berechtigen nie zum Signal; sie werden verworfen. Datenbank, Aufgaben, Logs und Budgets
bleiben erhalten. Bereits laufende Altprozesse ohne Identitätsbeleg müssen nach manueller
Prüfung separat beendet werden. Eine native Crew wird zusätzlich mit `tares factory crew
down` beendet. Signaturen, Produktionsfreigabe und Produktionsdeployments bleiben aus.

Grundlagen: [Codex-Berechtigungen](https://learn.chatgpt.com/docs/config-file/config-reference),
[App-Server](https://learn.chatgpt.com/docs/app-server),
[Tares-Crew](https://docs.glassflow.ai/tares/software-factory/get-started).

Git-Snapshots und Commits verwenden rohe Objekte ohne Checkout-/Clean-/Smudge-Filter;
Hooks sind bei Controller-Mutationen abgeschaltet. Zusätzliche Regressionen prüfen
feindliche Hooks/Filter und unveränderte Bytes trotz Export-Attributen.

Die Berechtigungswurzel stammt aus dem gepinnten Controller-Worktree. Neu angelegte
verschachtelte Git-Metadaten werden vor Checks/Review abgewiesen; sie können die
Lesefreigaben des Reviewers nicht erweitern. Git-Kontrollmetadaten bleiben schreibgeschützt.
