import { useState } from "react";
import { Card, CardContent, CardHeader } from "./ui/card";
import { Input } from "./ui/input";
import { Button } from "./ui/button";
import { MessageSquareText } from "lucide-react";

export default function Login({ onLogin }) {
  const [name, setName] = useState("");

  function submit(e) {
    e.preventDefault();
    if (!name.trim()) return;
    // The API is always reached same-origin, since nginx forwards /chat to it.
    onLogin(name.trim(), "");
  }

  return (
    <div className="flex h-full items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <div className="flex items-center gap-2">
            <div className="flex size-8 items-center justify-center rounded-lg bg-primary text-primary-foreground">
              <MessageSquareText className="size-4" />
            </div>
            <span className="text-lg font-semibold">ChatDemo</span>
          </div>
          <p className="text-sm text-muted-foreground">Enter a user name to start.</p>
        </CardHeader>
        <CardContent>
          <form onSubmit={submit} className="flex flex-col gap-3">
            <div className="flex flex-col gap-1.5">
              <label htmlFor="username" className="text-sm font-medium">
                User name
              </label>
              <Input
                id="username"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="your name"
                autoFocus
              />
            </div>
            <Button type="submit" className="w-full">
              Continue
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
