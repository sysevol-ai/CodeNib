// SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
//
// SPDX-License-Identifier: Apache-2.0

package main

import "fmt"

// Greeter is an interface for greeting.
type Greeter interface {
	SayHello(name string) string
}

// SimpleGreeter implements the Greeter interface.
type SimpleGreeter struct {
	Prefix string
}

// SayHello returns a greeting message.
func (g *SimpleGreeter) SayHello(name string) string {
	return fmt.Sprintf("%s, %s!", g.Prefix, name)
}

// Greet is a package-level function that creates a greeting.
func Greet(name string) string {
	g := &SimpleGreeter{Prefix: "Hello"}
	return g.SayHello(name)
}
