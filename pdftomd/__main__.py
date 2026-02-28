import typer, os
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, MofNCompleteColumn
from concurrent.futures import ProcessPoolExecutor, as_completed
from .converter import convert

app = typer.Typer(help="pdftomd: Heuristic PDF to Markdown Converter")
console = Console()

# Number of parallel workers for bulk conversion
DEFAULT_WORKERS = min(4, os.cpu_count() or 1)

@app.command()
def main(
    input_path: str = typer.Argument(..., help="PDF file or folder to convert"),
    output: str = typer.Option(None, "--output", "-o", help="Output file (single PDF) or folder (bulk)"),
    recursive: bool = typer.Option(False, "--recursive", "-r", help="Recurse into subdirectories"),
    workers: int = typer.Option(DEFAULT_WORKERS, "--workers", "-w", help="Parallel workers for bulk conversion"),
    page_breaks: bool = typer.Option(False, "--page-breaks", help="Insert --- between pages"),
):
    if os.path.isfile(input_path):
        _run_file(input_path, output, page_breaks)
    else:
        _run_dir(input_path, output, recursive, workers)

def _run_file(path, out, page_breaks=False):
    out = out or os.path.splitext(path)[0] + ".md"
    with Progress(SpinnerColumn(), TextColumn("[cyan]Converting {task.fields[fn]}..."), console=console) as p:
        p.add_task("", fn=os.path.basename(path))
        md = convert(path, page_breaks=page_breaks)
        with open(out, "w", encoding="utf-8") as f: f.write(md)
    console.print(f"[bold green]✓[/] Created: {out}")

def _run_dir(path, out_dir, rec, workers):
    files = []
    for root, _, fnames in os.walk(path):
        for fn in fnames:
            if fn.lower().endswith(".pdf"): files.append(os.path.join(root, fn))
        if not rec: break
    
    if not files:
        console.print("[yellow]No PDF files found.[/]")
        return
    
    out_dir = out_dir or "markdown_results"
    os.makedirs(out_dir, exist_ok=True)

    # Pro Feature #7: Multiprocessing for bulk conversion
    if workers > 1 and len(files) > 1:
        _run_dir_parallel(files, path, out_dir, workers)
    else:
        _run_dir_sequential(files, path, out_dir)


def _convert_single(args):
    """Worker function for parallel processing."""
    src, dest = args
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        md = convert(src)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(md)
        return (src, True, None)
    except Exception as e:
        return (src, False, str(e))


def _run_dir_parallel(files, base_path, out_dir, workers):
    """Bulk conversion with multiprocessing (Pro Feature #7)."""
    # Prepare task list
    tasks = []
    for f in files:
        rel = os.path.relpath(f, base_path)
        dest = os.path.join(out_dir, os.path.splitext(rel)[0] + ".md")
        tasks.append((f, dest))
    
    console.print(f"[cyan]Converting {len(files)} files with {workers} workers...[/]")
    
    completed = 0
    errors = []
    
    with Progress(
        TextColumn("[bold blue]{task.fields[fn]}"),
        BarColumn(),
        MofNCompleteColumn(),
        console=console
    ) as progress:
        task = progress.add_task("Converting...", total=len(files), fn="Files")
        
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_convert_single, t): t for t in tasks}
            
            for future in as_completed(futures):
                src, success, err = future.result()
                completed += 1
                progress.update(task, completed=completed, fn=os.path.basename(src))
                
                if not success:
                    errors.append((src, err))
    
    if errors:
        console.print(f"\n[red]Failed: {len(errors)} files[/]")
        for src, err in errors[:5]:  # Show first 5 errors
            console.print(f"  [dim]{os.path.basename(src)}:[/] {err}")
    
    console.print(f"[bold green]✓[/] Converted {len(files) - len(errors)}/{len(files)} files to {out_dir}/")


def _run_dir_sequential(files, base_path, out_dir):
    """Original sequential bulk conversion."""
    with Progress(TextColumn("[bold blue]{task.fields[fn]}"), BarColumn(), MofNCompleteColumn(), console=console) as p:
        t = p.add_task("Bulk...", total=len(files), fn="Files")
        for f in files:
            p.update(t, fn=os.path.basename(f))
            rel = os.path.relpath(f, base_path)
            dest = os.path.join(out_dir, os.path.splitext(rel)[0] + ".md")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as fo: fo.write(convert(f))
            p.advance(t)

if __name__ == "__main__":
    app()
