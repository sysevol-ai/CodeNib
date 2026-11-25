import { FolderOpen } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Switch } from "@/components/ui/switch";
import { backend, message } from "@/lib/backend";
import { useStore } from "@/lib/store";

export function AddRepoDialog() {
  const { addFolder, setAddFolder, addRepository } = useStore();
  const [analysis, setAnalysis] = useState(true);
  const [generate, setGenerate] = useState(false);
  const [busy, setBusy] = useState(false);

  const add = async () => {
    if (!addFolder) return;
    setBusy(true);
    try {
      await addRepository(addFolder, analysis, generate);
      setAddFolder(null);
    } catch (error) {
      toast.error(message(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={addFolder !== null} onOpenChange={(o) => !o && setAddFolder(null)}>
      <DialogContent className="add-dialog">
        <DialogHeader>
          <DialogTitle>Add a repository</DialogTitle>
          <DialogDescription>CodeNib reads it on this Mac. The code stays where it is.</DialogDescription>
        </DialogHeader>
        <div className="add-field">
          <span className="add-label">Folder</span>
          <div className="add-folder">
            <span className="mono">{addFolder}</span>
            <Button
              variant="outline"
              size="sm"
              onClick={() => backend.pickFolder().then((f) => f && setAddFolder(f)).catch((e) => toast.error(message(e)))}
            >
              <FolderOpen />
              Choose
            </Button>
          </div>
        </div>
        <label className="add-option">
          <Checkbox checked disabled />
          <span>
            <strong>Start from README and docs</strong>
            <span className="add-sub">Ready in seconds. Always on.</span>
          </span>
        </label>
        <label className="add-option">
          <Switch checked={analysis} onCheckedChange={setAnalysis} />
          <span>
            <strong>Build analysis tools</strong>
            <span className="add-sub">Index, symbol graph and map. A few minutes, no model.</span>
          </span>
        </label>
        <label className="add-option">
          <Switch checked={generate} onCheckedChange={setGenerate} />
          <span>
            <strong>Write pages with Claude Code</strong>
            <span className="add-sub">About $0.10 to $0.20 a page, on your Claude Code plan.</span>
          </span>
        </label>
        <DialogFooter>
          <Button variant="ghost" onClick={() => setAddFolder(null)}>
            Cancel
          </Button>
          <Button onClick={() => void add()} disabled={busy || !addFolder}>
            {busy ? "Adding…" : "Add repository"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
