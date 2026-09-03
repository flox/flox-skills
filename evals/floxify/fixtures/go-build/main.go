package main

import (
	"flag"
	"fmt"
)

func main() {
	name := flag.String("name", "World", "who to greet")
	flag.Parse()
	fmt.Printf("Hello, %s!\n", *name)
}
