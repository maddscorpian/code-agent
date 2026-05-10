import * as vscode from "vscode";

export class LocalAiCodeLensProvider implements vscode.CodeLensProvider {
  provideCodeLenses(document: vscode.TextDocument): vscode.CodeLens[] {
    const lenses: vscode.CodeLens[] = [];
    const text = document.getText();
    const addLens = (line: number, title: string, question: string) => {
      lenses.push(
        new vscode.CodeLens(new vscode.Range(line, 0, line, 0), {
          title,
          command: "localai.openChatFromLens",
          arguments: [question],
        })
      );
    };

    const lines = text.split("\n");
    lines.forEach((ln, i) => {
      if (/@Component/.test(ln)) {
        addLens(i, "Explain Component", "Explain this Angular component");
        addLens(i, "What calls this?", "What calls this component?");
      }
      if (/@Injectable/.test(ln)) {
        addLens(i, "Explain Service", "Explain this Angular service");
        addLens(i, "Show HTTP calls", "Show all HTTP calls in this service");
      }
      if (/@RestController/.test(ln)) {
        addLens(i, "Show Endpoints", "Show endpoints in this controller");
        addLens(i, "Explain Controller", "Explain this Spring controller");
      }
      if (/@Entity/.test(ln)) {
        addLens(i, "Show Schema", "Show entity schema");
        addLens(i, "What uses this entity?", "What uses this entity?");
      }
      if (/@(?:GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping)/.test(ln)) {
        addLens(i, "Explain Endpoint", "Explain this endpoint");
        addLens(i, "Generate Test", "Generate a test for this endpoint");
      }
    });
    return lenses;
  }
}
