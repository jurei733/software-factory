# Allgemeine GitHub-Anbindung

Dieses Overlay ist eigenständig. Es lädt keine private zentrale Factory und benötigt keinen
zusätzlichen GitHub- oder KI-Token. Es verändert das Quellrepository beim Export nicht.

1. `.factory/config.json` prüfen: Repository `owner/name`, Regeln, Laufzeit, Installation,
   Lint, Tests, Build, Zeitbudgets und Ausgabeordner. `cwd` erlaubt auch Monorepo-Unterprojekte.
   Node-Vorschläge verwenden npm nur mit vorhandenem Lockfile. pnpm, Yarn, andere Sprachen
   und besondere Toolchains über eigene Argumentlisten und Installationsschritte konfigurieren.
   Python-Vorschläge erwarten die im Projekt deklarierten dev-Werkzeuge ruff, pytest und build;
   andere Test- und Paketierwerkzeuge ausdrücklich ersetzen. Platzhalter scheitern immer.
2. Vorhandene Workflow-/Dateinamen auf Konflikte prüfen. Dateien im eigenen Pilotbranch
   einzeln übernehmen. Keine bestehenden Regeln, Templates oder Workflows überschreiben.
3. `python3 .factory/factory.py doctor --repo . --config .factory/config.json` ausführen.
   Der Befehl prüft Pfade und Schema ohne Projektcode auszuführen. Danach reale Befehle
   prüfen und im Profil `reviewed: true` setzen. Ohne diese Prüfung bleibt CI gesperrt.
4. PR öffnen. **Repository factory** prüft PRs, Merge-Queue-Ereignisse und Pushes auf den
   tatsächlichen Standardbranch (auch `master`, `develop` usw.). Installation und Prüfungen
   laufen seriell auf einem frischen GitHub-hosted Linux-Runner mit `contents: read` und
   ohne gespeicherte Checkout-Anmeldung. Der unabhängige Scanner führt keinen Projektcode aus.
   Trivy prüft Abhängigkeiten und Secrets; npm-Advisories ergänzen alle getrackten npm-Lockfiles.
   Alle HIGH-/CRITICAL- und Geheimnisfunde blockieren. Der gemeinsame Status heißt
   **Repository factory gate**; bei verfügbarem Branchschutz als Pflichtstatus eintragen.
5. Optional unter `ci.artifact_paths` konkrete Ausgabeordner eintragen. Nur reguläre Dateien
   bis insgesamt 200 MiB / 10000 Dateien werden als kurzlebige CI-Daten mit SHA-256 abgelegt.
   Fehlende Ausgaben, Links, fehlgeschlagene Tests und Upload-Fehler lassen die Prüfung scheitern.
6. **Factory task handoff** erzeugt manuell ein begrenztes Aufgabenpaket für die vorhandene
   lokale Claude-/Codex-Anmeldung. Die Beispielaufgabe ist rein lesend. Für Änderungen
   Ziel, Abnahme und konkrete `allowed_paths` festlegen. Das Paket startet keinen Agenten.

Lokal bleibt `factory.py run` ein eigener Docker-Prüfweg mit freigegebenem Werkzeugimage
per Digest und ohne Netzwerk/Host-Zugangsdaten. Der GitHub-hosted CI-Weg ist eine andere
Ausführungsumgebung: er benötigt Netzwerk zur Installation und bietet keine lokale
Container-/Pfadisolation. Self-hosted Runner werden vom CI-Controller abgewiesen.

Private Repositories laufen im jeweiligen Repository mit dessen eigenem `GITHUB_TOKEN`.
Zugriff, Actions-Freigabe und verfügbare Actions-Kontingente müssen dort vorhanden sein;
organisationsweite Action-Allowlisten können die gepinnten Aktionen zunächst sperren.
CI-Daten werden nicht automatisch an die zentrale Factory übertragen. Bestehende
Nutzungsbudgets bleiben wirksam; der Export bucht keinen Tarif und erhöht kein Budget.

Das Overlay aktiviert keine Pflicht-Zweitfreigabe, Branchregeln, Signaturen, Releases oder
Deployments. CI-Prüfsummen sind keine signierte Build-Herkunft. Produktionsfreigabe,
Staging, Berechtigungen, Datenklassifikation und Sicherungen benötigen ein eigenes Profil
und einen nachgewiesenen Betriebspfad. Private Quelltexte dürfen nur entsprechend eurer
Freigabe in die lokale KI-Sitzung gelangen; der Workflow überträgt keine Anbieteranmeldung.
