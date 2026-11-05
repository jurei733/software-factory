# Software Factory

Eine portable Softwarefabrik für GitHub-Repositories: Projekt inventarisieren, prüfbare
Aufträge für Codex oder Claude vorbereiten, Änderungen isoliert prüfen und Ergebnisse
mit nachvollziehbaren Belegen zur menschlichen Freigabe übergeben.

Die Factory benötigt keine kostenpflichtige Buchung und keine Anbieter-API-Schlüssel.
Sie nutzt vorhandene persönliche Assistentenanmeldungen. CI und Security laufen unabhängig
von einer KI-Anmeldung. Ein grüner Build erteilt keine Produktionsfreigabe.

## Eigenes Repository anbinden

Voraussetzungen: Python >=3.9 und Git. Das Onboarding liest einen lokalen Checkout und
führt keine Projektbefehle aus. Ein neues Overlay entsteht außerhalb des Projekts:

```sh
python3 factory.py inspect --repo /pfad/zum/projekt
python3 factory.py onboard --repo /pfad/zum/projekt \
  --repository dein-konto/dein-projekt --out /pfad/zum/neuen-overlay
```

Das erzeugte `.factory/config.json` nennt Repository, Projektregeln, Befehle, Laufzeiten,
Zeitbudgets und Build-Ausgaben. Prüfe diese Angaben, ergänze fehlende Schritte und setze
erst danach `reviewed` auf `true`. Übernimm das Overlay in einen eigenen Projektbranch.

```sh
python3 factory.py doctor --repo /pfad/zum/projekt \
  --config /pfad/zum/neuen-overlay/.factory/config.json
python3 factory.py stage --config /pfad/zum/neuen-overlay/.factory/config.json \
  --out /pfad/zu/einem/weiteren-neuen-overlay
```

Node-Projekte mit npm-, pnpm- oder Yarn-Lockfile sowie Python-Projekte erhalten Vorschläge.
Bei pnpm/Yarn muss `packageManager` eine konkrete Version nennen. Fehlende Befehle bleiben
sichtbar gesperrt. Andere Sprachen, Make/CMake und Monorepos nutzen `--stack custom` mit
expliziten Installations- und Prüfschritten, jeweils als Argumentliste und mit eigenem `cwd`.
Benötigte Toolchains müssen in der CI eingerichtet oder im lokalen Werkzeugimage enthalten sein.

Jedes GitHub-Repository lässt sich über ein solches Profil anbinden. Die Factory kann nicht
für unbekannte Projekte passende Tests oder Toolchains erfinden und meldet fehlende Konfiguration
als Fehler. Ein Profil ist an sein Zielrepository gebunden und wird in einem anderen Repository
abgewiesen. Das Selbstprofil der Factory bindet sich ausdrücklich an `$GITHUB_REPOSITORY`.

## Agentenauftrag und isolierte Abnahme

```sh
python3 factory.py prepare --config /pfad/config.json --task /pfad/task.json \
  --assistant codex --out /pfad/zum/neuen-arbeitspaket
python3 factory.py run --repo /pfad/zum/sauberen-checkout \
  --config /pfad/config.json --task /pfad/task.json \
  --image registry.example/toolchain@sha256:DEIN_GEPRUEFTER_DIGEST \
  --patch /pfad/aenderung.patch --report /pfad/ausserhalb/checks.json
```

Das Arbeitspaket startet keinen Agenten. Codex oder Claude bearbeitet den Auftrag über die
vorhandene persönliche Anmeldung. Die lokale Abnahme führt Projektcode ausschließlich in
Docker aus: kein Netzwerk, keine Host-Zugangsdaten, nicht-root, schreibgeschützte Quellen
mit ausdrücklich erlaubten Ticketpfaden, Ressourcen- und Versuchsbudgets. Es gibt keinen
Fallback zur Ausführung auf dem Host. Werkzeugimages benötigen einen unveränderlichen Digest.

Die lokale Abnahme verlangt einen sauberen Checkout mit den **rohen Commit-Bytes und
Dateimodi**. Smudge-/LFS-/EOL-transformierte Arbeitskopien werden ausdrücklich abgewiesen.
Globale Git-Konfiguration, globale Ignore-Regeln, Hooks, Filter und Templates werden nicht
geerbt; benötigte Ignore-Regeln gehören in das Projekt. Exportattribute verändern die
Prüfquelle nicht. Binäre Änderungen, neue/gelöschte Dateien und interne Links werden anhand
des vollständigen Dateibaums geprüft; Änderungen außerhalb der Aufgabe bleiben gesperrt.

Die exportierte GitHub-CI verwendet frische Standard-Runner mit Lesezugriff. Projektcode und
Security-Analyse laufen in getrennten Jobs; Installationsfehler verhindern Tests/Build/Pakete.
HIGH/CRITICAL-Abhängigkeiten und Secret-Funde sperren das Gate. Vorgeschlagene Befehle müssen
vor dem Aktivieren geprüft werden. Ein unterstütztes öffentliches Repository kann Standard-
Runner kostenfrei nutzen; Konto- und Speicherbeschränkungen sind separat zu prüfen.

## Optionaler lokaler Agentenpilot

[Tares-Pilot](templates/TARES_PILOT.md) bietet drei synthetische Übungen, begrenzte lokale
Codex-Arbeit, lesendes Review und einen deterministischen GitHub-Publisher ohne Modelllauf.
Der Repository-Name wird explizit mit `--repository dein-konto/dein-projekt` gewählt.
Der Pilot ist eine geprüfte Demonstration; allgemeine Projektaufträge verwenden das Profil
und das Arbeitspaket oben. Eine native Claude-Crew benötigt die eigene bestehende Berechtigung
und Anmeldung des jeweiligen Kollegen. Ohne diese Berechtigung bleibt sie ausgeschaltet.

## Betrieb und Freigabe

Factory-CI, Agentenpilot, Projekt-CI und Aufgabenübergabe funktionieren ohne fest eingetragenen
GitHub-Besitzer oder Repository-Namen. Build-Pakete werden nur vom konfigurierten Defaultbranch
als unveröffentlichte Release-Entwürfe gespeichert. Die Factory besitzt einen signierten
Releasepfad mit gebundenen Sicherheitsbelegen/SBOM, verifizierter Paketinstallation und
manuell ausgelöster Veröffentlichung. Branchregeln müssen im jeweiligen Repository aktiviert
sein; ein zweiter Code Owner ist optional. Andere Anwendungen werden nicht automatisch deployt.

Versionierte Integrationen lassen sich mit `factory.py upgrade --repo ... --out ...`
zunächst als Vorschau aktualisieren. `--apply` prüft Konflikte, erhält eigene Profile/Aufgaben
und legt externe Sicherungen an. [Betrieb, Releases und Wiederherstellung](docs/OPERATIONS.md),
[Änderungen](CHANGELOG.md), [Beiträge](CONTRIBUTING.md), [MIT-Lizenz](LICENSE).

```sh
python3 -m unittest discover -s tests -v
FACTORY_DOCKER_TESTS=1 python3 -m unittest discover -s tests -p test_isolation.py -v
FACTORY_DOCKER_TESTS=1 python3 -m unittest discover -s tests -p test_pilot_isolation.py -v
FACTORY_CODEX_TESTS=1 python3 -m unittest discover -s tests -p test_codex_boundaries.py -v
python3 scripts/package.py
```

Das portable Paket liegt unter `dist/software-factory.zip`.
[Security-Regeln](SECURITY.md), [Integration](templates/REPOSITORY_INTEGRATION.md),
[Controller-Grenzen](docs/controller-boundaries.md).
