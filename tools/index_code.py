"""Extract an accurate index of every module / class / function from src/ via AST."""
import ast
import pathlib

ROOT = pathlib.Path(r"C:\Redesign\BI_report_migration")
SRC = ROOT / "src"


def first_line(doc):
    if not doc:
        return ""
    return " ".join(doc.strip().split("\n")[0].split())


def sig(node):
    a = node.args
    names = [x.arg for x in a.posonlyargs + a.args]
    if a.vararg:
        names.append("*" + a.vararg.arg)
    names += [x.arg for x in a.kwonlyargs]
    if a.kwarg:
        names.append("**" + a.kwarg.arg)
    return "(" + ", ".join(names) + ")"


total_f = total_c = 0
for path in sorted(SRC.rglob("*.py")):
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    rel = path.relative_to(ROOT).as_posix()
    lines = len(text.splitlines())
    mod_doc = first_line(ast.get_docstring(tree))
    print(f"\n=== {rel}  ({lines} lines) ===")
    if mod_doc:
        print(f"    MODULE: {mod_doc}")
    consts = [n.targets[0].id for n in tree.body
              if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
              and n.targets[0].id.isupper()]
    if consts:
        print(f"    CONSTANTS: {', '.join(consts)}")
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            total_c += 1
            deco = [d.id if isinstance(d, ast.Name) else getattr(d, "attr", "") for d in node.decorator_list]
            bases = [b.id if isinstance(b, ast.Name) else getattr(b, "attr", "?") for b in node.bases]
            kind = "dataclass" if "dataclass" in str(deco) else ("Enum" if "Enum" in bases else ("Exception" if "Exception" in bases else "class"))
            print(f"  [{kind}] {node.name}: {first_line(ast.get_docstring(node))}")
            if kind == "Enum":
                vals = [t.targets[0].id for t in node.body if isinstance(t, ast.Assign)]
                print(f"      members: {', '.join(vals)}")
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    total_f += 1
                    prop = any(getattr(d, "id", "") == "property" for d in sub.decorator_list)
                    print(f"      {'@property ' if prop else ''}{sub.name}{sig(sub)}: {first_line(ast.get_docstring(sub))}")
                elif isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name) and kind == "dataclass":
                    ann = ast.unparse(sub.annotation)
                    print(f"      . {sub.target.id}: {ann}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            total_f += 1
            print(f"  def {node.name}{sig(node)}: {first_line(ast.get_docstring(node))}")

print(f"\n\nTOTALS: {total_c} classes, {total_f} functions/methods")
