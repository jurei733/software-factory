# Security policy

Projektquellen, Agentenantworten, Aufgabenbeschreibungen und Patchdateien sind Eingaben;
sie dürfen keine Berechtigungen, Zielrepositories oder Prüfregeln erweitern.

- Onboarding inventarisiert Dateien ohne Projektcode, Installationen oder Hooks auszuführen.
- Allgemeine Profile nennen ein explizites `owner/repository`. Nur das absichtlich portable
  Selbstprofil darf `$GITHUB_REPOSITORY` verwenden; der Wert stammt aus dem GitHub-Kontext.
- Lokale Projektprüfungen laufen ausschließlich in kurzlebigen Docker-Containern ohne
  Netzwerk, Host-Zugangsdaten oder Docker-Socket. Quellen sind schreibgeschützt, außer den
  ausdrücklich freigegebenen Ticketpfaden und leeren Scratch-Verzeichnissen.
- Die GitHub-CI nutzt frische Standard-Runner. Projektkommandos erhalten keine GitHub- oder
  Anbieter-Tokens. Security läuft getrennt und ohne Projektbefehle; HIGH/CRITICAL und
  Secret-Funde sperren die Freigabe. Fehlende Checks bleiben Fehler.
- Codex-Worker und Reviewer nutzen explizite Dateisystemprofile mit begrenzten Lesewurzeln.
  Der Worker darf nur Ticketpfade schreiben; Git-Kontrollmetadaten bleiben schreibgeschützt.
- Der Publisher startet keinen Modelllauf. Er prüft vor jeder Mutation Tool, Repository,
  Ref, Dateiinhalte und Reihenfolge. Ergebnisbaum, genau ein gepinnter Elterncommit, Head,
  PR-Ziel und Aktualität des Ausgangsbranches müssen übereinstimmen.
- Rohe Git-Objekte verhindern die Ausführung von Checkout-/Clean-/Smudge-Filtern. Hooks sind
  für Controller-Mutationen abgeschaltet. Verschachtelte Git-Metadaten werden abgewiesen.
- Runtime, Token, Logs, Prozessidentitäten und Budgets liegen außerhalb jedes Checkouts und
  des entpackten Factory-Pakets. Status erzeugt keine Zugangsdaten. Abgebrochene Prozesse
  werden identifiziert beendet und reappt; gespeicherte Versuche bleiben verbraucht.
- Vorhandene persönliche Codex-/Claude-Anmeldungen bleiben lokal. Keine Tokens anderer
  Kollegen, Anbieter-API-Schlüssel, kostenpflichtigen Fallbacks oder automatischen Zukäufe.
- Pakete sind geprüfte CI-Daten und keine Produktionsfreigabe. Produktionsattestierung
  erfordert explizit gepinntes Repository, Workflow, Defaultbranch, Commit und saubere Evidenz.
  Keine automatischen Produktionsdeployments und kein Auto-Merge.

Branchschutz und Signaturen werden nicht allein durch Vorlagendateien aktiviert. Ein zweiter
Code Owner oder ein zweites menschliches Review ist optional. Geheimnisse und vertrauliche
Produktionsdaten gehören weder in das Repository noch in Tasks, Logs oder Build-Ausgaben.
Eine Veröffentlichung macht auch Commit-Historie, PR-Texte und Actions-Logs öffentlich.

Sicherheitsprobleme bitte direkt über den Security-Bereich des betroffenen Repositorys melden;
Zugangsdaten und ausnutzbare Details nicht in öffentliche Issues schreiben.
