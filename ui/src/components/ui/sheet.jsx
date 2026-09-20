import * as React from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { XIcon } from "lucide-react";
import { cn } from "../../lib/utils";

function Sheet({ ...props }) {
  return <DialogPrimitive.Root {...props} />;
}

function SheetTrigger({ ...props }) {
  return <DialogPrimitive.Trigger {...props} />;
}

function SheetClose({ ...props }) {
  return <DialogPrimitive.Close {...props} />;
}

function SheetOverlay({ className, ...props }) {
  return (
    <DialogPrimitive.Overlay
      className={cn(
        "fixed inset-0 z-50 bg-black/40",
        "data-[state=open]:animate-in data-[state=open]:fade-in-0",
        "data-[state=closed]:animate-out data-[state=closed]:fade-out-0",
        className,
      )}
      {...props}
    />
  );
}

function SheetContent({ className, children, side = "right", ...props }) {
  return (
    <DialogPrimitive.Portal>
      <SheetOverlay />
      <DialogPrimitive.Content
        className={cn(
          "fixed z-50 flex flex-col gap-4 bg-card text-card-foreground shadow-lg border-border transition ease-in-out",
          "data-[state=open]:animate-in data-[state=closed]:animate-out",
          side === "right" &&
            "inset-y-0 right-0 h-full w-full max-w-sm border-l data-[state=closed]:slide-out-to-right data-[state=open]:slide-in-from-right",
          className,
        )}
        {...props}
      >
        {children}
        <DialogPrimitive.Close className="absolute right-3 top-3 rounded-sm opacity-70 outline-none transition-opacity hover:opacity-100 focus-visible:ring-2 focus-visible:ring-ring">
          <XIcon className="size-4" />
          <span className="sr-only">Close</span>
        </DialogPrimitive.Close>
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
}

function SheetHeader({ className, ...props }) {
  return <div className={cn("flex flex-col gap-1 p-4 border-b border-border", className)} {...props} />;
}

function SheetTitle({ className, ...props }) {
  return (
    <DialogPrimitive.Title
      className={cn("text-sm font-semibold text-foreground", className)}
      {...props}
    />
  );
}

export { Sheet, SheetTrigger, SheetClose, SheetContent, SheetHeader, SheetTitle };
