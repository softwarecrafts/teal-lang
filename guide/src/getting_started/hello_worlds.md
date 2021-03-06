# Hello Worlds!

Hello world in teal looks like this. Type or paste this in your favourite editor.

```javascript
// service.tl

fn hello_world() {
  "Hello from teal!"
}
```

Which we can run locally:

```bash
teal service.tl -f hello_world
```

## Explanation

Teal files contain imports and teal functions. The command line program allows us to specify which teal function to invoke with the `-f` argument. The age old `main` function convention also applies in teal.

Our `service.tl` when run without an explicit function will tell us that we dont have a `main` function.

```bash
teal service.tl
```
```
Can't run function `main'.
Does it exist in service.tl?
```

We can solve that easily by adding a main.

```javascript
// service.tl

fn hello_world() {
  "Hello from teal!"
}

fn main() {
  hello_world()
}
```
