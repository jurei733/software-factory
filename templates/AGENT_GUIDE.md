# Regeln für einen Factory-Auftrag

Die bestehenden AGENTS.md, verschachtelten Agentenregeln und gegebenenfalls rules.md zuerst lesen.
Diese Datei ist ein Vorschlag zur Ergänzung dieser Regeln und ersetzt sie nicht.

1. Aufgabe mit Ziel, Ausschlüssen, Abnahmekriterien, erlaubten Pfaden und Risiko beschreiben.
2. Öffentlichen Code und synthetische Testdaten verwenden. Zugangsdaten, personenbezogene Daten,
   unveröffentlichte Aufgaben und produktive Antwortdaten gehören nicht in dieses Pilotverfahren.
3. In isoliertem Branch/Worktree arbeiten. Vor Änderungen die vorhandenen lokalen Änderungen prüfen.
4. Zuerst einen kurzen Lösungsplan erstellen, dann eine kleine, überprüfbare Änderung umsetzen.
5. Aussagekräftige Tests für geändertes Verhalten ergänzen und die projektspezifischen Checks ausführen.
   Nicht ausgeführte oder fehlgeschlagene Checks ausdrücklich nennen.
6. Prüfungen oder Freigaben nicht abschwächen, um einen Auftrag erfolgreich erscheinen zu lassen.
7. Bei neuen Abhängigkeiten, Datenmigrationen, Berechtigungsänderungen oder geänderten
   fachlichen Ergebnissen Verantwortliche einbeziehen. High-risk-Aufträge zuerst entwerfen und prüfen lassen.
8. Nach spätestens dem vereinbarten Zeit-/Versuchsbudget mit einem überprüfbaren Zwischenstand stoppen.
9. Diff, Testnachweise, Einschränkungen und Rücknahmeweg für ein menschliches Review liefern.
10. Merge, Release und Produktionsbetrieb bleiben bei den hierfür verantwortlichen Menschen.


Die native Claude-/Codex-Sitzung bleibt beim vorhandenen Login und benötigt ihre eigenen Zugriffsregeln.
Die Factory übernimmt nur einen Patch innerhalb allowed_paths. factory.py run prüft ihn in einem frischen,
isolierten Docker-Arbeitsbereich ohne Netzwerk oder Host-Zugangsdaten. Das Originalcheckout bleibt unverändert.
Erlaubte Schreibpfade, Prozess-/CPU-/RAM-Grenzen sowie Zeit- und Versuchsbudgets werden für diese Prüfsitzung
technisch erzwungen. Ein Coding-Assistent selbst wird damit nicht automatisch gestartet oder global eingeschränkt.
